"""`PUT /config` のユースケース（T-13。設計書 §3.3・§4.3 ／ 仕様書 §7.2・§7.4）。

管理画面（T-33）から届く **部分更新（patch）** を現行 config へ当て、検証を通し、
保存し、監査ログを残すまでを1本にまとめる。

---

**設計上、動かしてはいけない点**

1. **許可リスト方式。** patch に載せてよいのは仕様書 §7.2 の編集可能パラメータ
   （`EDITABLE_PATHS`）だけ。ID系・`scoring_total`・`schema_version`・`meta`・
   `enums` などを含む patch は **422 で拒否**し、**一部だけ適用しない**
   （1件でも違反があれば何も書かない）。
2. **`meta` は patch から触れない。** revision を呼び出し元に決めさせると
   楽観ロックが成立しない（T-11 の `save()` がサーバ値で上書きする）。
   patch に `meta` を含めた時点で 422 にして、意図を早く伝える。
3. **保存の関門は T-05。** 配点合計100・しきい値の降順整合などは
   `ConfigRepository.save()` が `ensure_valid_config()` で強制する。
   **不正なら保存を拒否し、値を自動補正しない**（設計判断A）。
   このモジュールに正規化処理を足さないこと。
4. **監査ログは config の書き込みと同じトランザクションに載せる。**
   `save()` が commit する前に `AuditLog` を積み、失敗時は rollback する。
   「config は変わったが誰が変えたか残っていない」状態を作らない（仕様書 §6.1）。

⚠️ **認可はここではしない。** config ファミリが admin 限定であること
（仕様書 §6.2・§6.1）は HTTP 層（`require_admin`）の責務で、ここは
「admin だと確定した呼び出し元」を `Principal` として受け取り、
`updated_by` と監査ログの `actor` に使うだけ。
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.config_repository import (
    ConfigRepository,
    ConfigRevisionConflictError,
    diff_configs,
)
from adapter.storage.artifact_store import CONFIG_FILENAME
from application.usecases.audit import AuditService
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.config_validation import ConfigIssue, ConfigIssueCode
from enterprise.entities.principal import Principal

# 配列セクションと「どの要素か」を示す識別子。
# `id` / `no` は**セレクタであって編集対象ではない**（ID系は変更不可。仕様書 §5.1）。
LIST_SELECTORS: dict[str, str] = {
    "information_categories": "id",
    "scoring_axes": "id",
    "exclusion_rules": "no",
}

# 仕様書 §7.2「編集可能パラメータ」。配列要素は `*` で表す。
# **ここに無いパスは編集できない**（設計書 §3.3「patch は §7.2 の編集可能
# パラメータのみ許可」）。増やすときは §7.2 の表も一緒に更新すること。
EDITABLE_PATHS: frozenset[str] = frozenset(
    {
        # スコア軸の配点（合計100の担保は T-05 ／ 設計判断A）
        "scoring_axes.*.weight",
        # 掲載最低スコア・採用区分しきい値（降順整合は T-05）
        "tunable_thresholds.min_total_score_to_publish",
        "tunable_thresholds.adoption_class_score_map.propose_next_meeting",
        "tunable_thresholds.adoption_class_score_map.reference_info",
        "tunable_thresholds.adoption_class_score_map.share_only",
        # 除外ルールの有効/無効・強度（仕様書 §5.4）
        "exclusion_rules.*.enabled",
        "exclusion_rules.*.severity",
        # カテゴリ優先度
        "information_categories.*.priority",
        # 週刊：対象業界（参照整合は T-05）・トピック上限
        "tunable_thresholds.weekly.target_industry",
        "tunable_thresholds.weekly.max_industry_topics",
        "tunable_thresholds.weekly.max_common_topics",
        # 月刊：目標事例数・章数
        "tunable_thresholds.monthly.target_case_count",
        "tunable_thresholds.monthly.chapter_count_hint",
        # 重複判定パラメータ（仕様書 §11.2）
        "tunable_thresholds.dedup.lookback_weeks",
        "tunable_thresholds.dedup.title_similarity_threshold",
        "tunable_thresholds.dedup.treat_same_url_as_duplicate",
        # ⚠️ ここから4件は **§7.2 の表に行が無い**が `tunable_thresholds` に
        # 属する項目。§7.2 の見出しが「§5.2 の**可変項目**にマップ」であり、
        # 可変項目の定義（仕様書 §5.1 ／ T-04）は `tunable_thresholds` を
        # まるごと可変としているため、表の取りこぼしと解釈して許可した。
        # 特に `min_reliability_score_to_publish` は採否判定（T-21 手順5）が
        # 使う値で、編集できないと config.json の手編集でしか変えられない。
        # **§7.2 の表に4行を追記する必要がある（→ T-38）。**
        "tunable_thresholds.min_reliability_score_to_publish",
        "tunable_thresholds.weekly.point_of_week_required",
        "tunable_thresholds.monthly.min_score_for_case",
        "tunable_thresholds.monthly.require_editorial_and_closing",
    }
)


def _proper_prefixes(path: str) -> set[str]:
    parts = path.split(".")
    return {".".join(parts[:index]) for index in range(1, len(parts))}


# 中間ノード（`tunable_thresholds` / `tunable_thresholds.weekly` 等）。
# patch がここまで降りてくるのは正当なので、`EDITABLE_PATHS` と区別する。
_EDITABLE_PREFIXES: frozenset[str] = frozenset(
    prefix for path in EDITABLE_PATHS for prefix in _proper_prefixes(path)
)


class ConfigPatchError(Exception):
    """patch が許可リスト・型・値域に反している（HTTP 層は 422 へ変換）。

    `issues` は `ConfigIssue`（T-05 と同じ形）なので、フロントは
    クロスフィールド違反と同じ方法で `path` をフォーム欄へ対応づけられる。

    **早期 return せず全違反を集めて返す**（一度の保存でまとめて直せるように。
    T-05 と同じ方針）。
    """

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        super().__init__(
            "; ".join(f"{issue.path}: {issue.reason}" for issue in issues)
            or "patch のバリデーションに失敗しました"
        )


def _not_editable_issue(path: str, *, exists: bool) -> ConfigIssue:
    """「編集できない」違反1件。config に在るキーかどうかで理由を分ける。"""
    if exists:
        return ConfigIssue(
            path=path,
            reason=(
                "この項目は編集できません（仕様書 §7.2 の編集可能パラメータ外）。"
                "ID系・scoring_total・schema_version・meta・enums は固定です"
            ),
            code=ConfigIssueCode.FIELD_NOT_EDITABLE,
        )
    return ConfigIssue(
        path=path,
        reason="config に存在しない項目です",
        code=ConfigIssueCode.UNKNOWN_FIELD,
    )


def apply_patch(
    current: IntelligenceConfig, patch: Mapping[str, Any]
) -> IntelligenceConfig:
    """現行 config に patch を当てた候補を作る。

    許可リスト（`EDITABLE_PATHS`）に載っていないパスが1つでもあれば
    **何も適用せず** `ConfigPatchError` を投げる。部分適用は、admin が
    「一部だけ通った」ことに気づけないまま次回フィルタが走る事故になるため。

    クロスフィールド制約（Σweight==100 等）はここでは見ない。保存直前に
    `ConfigRepository.save()` が T-05 を通す（責務を1箇所に保つ）。

    Args:
        current: 現行 config（patch の適用先）
        patch: 部分更新。`{"scoring_axes":[{"id":"...","weight":25}], ...}`

    Returns:
        patch 適用後の候補 config（`meta` は現行のまま。保存時に打ち直される）

    Raises:
        ConfigPatchError: 許可外パス・未知キー・要素指定の誤り・型/値域違反
    """
    data = current.model_dump(mode="json")
    issues: list[ConfigIssue] = []
    _apply_mapping(patch, data, prefix="", issues=issues)
    if issues:
        raise ConfigPatchError(issues)

    try:
        return IntelligenceConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigPatchError(
            [
                ConfigIssue(
                    path=".".join(str(part) for part in error["loc"]),
                    reason=error["msg"],
                    code=ConfigIssueCode.INVALID_VALUE,
                )
                for error in exc.errors()
            ]
        ) from exc


def _apply_mapping(
    patch: Mapping[str, Any],
    target: dict[str, Any],
    *,
    prefix: str,
    issues: list[ConfigIssue],
) -> None:
    for key, value in patch.items():
        path = f"{prefix}.{key}" if prefix else key

        if path in LIST_SELECTORS:
            _apply_list(value, target[key], section=path, issues=issues)
            continue

        if path in EDITABLE_PATHS:
            target[key] = value
            continue

        if path in _EDITABLE_PREFIXES:
            if not isinstance(value, Mapping):
                issues.append(
                    ConfigIssue(
                        path=path,
                        reason="オブジェクトで指定してください",
                        code=ConfigIssueCode.INVALID_VALUE,
                    )
                )
                continue
            _apply_mapping(value, target[key], prefix=path, issues=issues)
            continue

        issues.append(_not_editable_issue(path, exists=key in target))


def _apply_list(
    entries: Any,
    target: list[dict[str, Any]],
    *,
    section: str,
    issues: list[ConfigIssue],
) -> None:
    """配列セクションへ patch を当てる。要素は `id` / `no` で対応づける。

    ⚠️ **添字では対応づけない。** 並び順は固定（T-05 の項目5）だが、フロントが
    フィルタ済みの一覧を送ってくると添字がずれる。識別子で引けば、順序に
    依存せず、存在しない要素は 422 として弾ける。
    """
    selector = LIST_SELECTORS[section]

    if not isinstance(entries, list):
        issues.append(
            ConfigIssue(
                path=section,
                reason="配列で指定してください",
                code=ConfigIssueCode.INVALID_VALUE,
            )
        )
        return

    index_by_selector = {item[selector]: index for index, item in enumerate(target)}

    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            issues.append(
                ConfigIssue(
                    path=f"{section}.{position}",
                    reason="要素はオブジェクトで指定してください",
                    code=ConfigIssueCode.INVALID_VALUE,
                )
            )
            continue

        if selector not in entry:
            issues.append(
                ConfigIssue(
                    path=f"{section}.{position}",
                    reason=f"どの要素かを示す {selector} が必要です",
                    code=ConfigIssueCode.UNKNOWN_TARGET,
                )
            )
            continue

        key = entry[selector]
        if not isinstance(key, str | int) or key not in index_by_selector:
            issues.append(
                ConfigIssue(
                    path=f"{section}.{position}.{selector}",
                    reason=(
                        f"{selector}={key!r} の要素が config にありません。"
                        "ID系は変更できないため、既存の要素を指定してください"
                        "（仕様書 §5.1）"
                    ),
                    code=ConfigIssueCode.UNKNOWN_TARGET,
                )
            )
            continue

        index = index_by_selector[key]
        for field, value in entry.items():
            if field == selector:
                # セレクタ自身は「どれか」の指定であって変更ではない。
                continue
            if f"{section}.*.{field}" in EDITABLE_PATHS:
                target[index][field] = value
                continue
            issues.append(
                _not_editable_issue(
                    f"{section}.{index}.{field}", exists=field in target[index]
                )
            )


def _now() -> datetime:
    """現在時刻（UTC・tz付き）。テストが差し替えられるよう1箇所に集約する。"""
    return datetime.now(UTC)


class UpdateConfigUsecase:
    """`PUT /config`：patch 適用 → 楽観ロック → 検証 → 保存 → 監査ログ。"""

    def __init__(self, db: AsyncSession, repo: ConfigRepository) -> None:
        self._db = db
        self._repo = repo
        self._audit = AuditService(db)

    async def execute(
        self,
        actor: Principal,
        *,
        base_revision: int,
        patch: Mapping[str, Any],
    ) -> IntelligenceConfig:
        """config を更新する。

        順序は設計書 §1.3 のシーケンスどおり:

        1. 現行 config を読む（無ければ `ConfigNotFoundError` → 404）
        2. **楽観ロック**：`base_revision` 不一致は 409。⚠️ patch を当てる前に
           見るのは、admin が見ていない config に対する項目別 422 を返しても
           直しようがないため（正しい案内は「読み直して再保存」）
        3. patch の許可リスト・型検査（422）
        4. 監査ログを積む（commit は 5 と同じトランザクション）
        5. 保存（T-05 のクロスフィールド検証 → 原子的書き込み → revision++）

        Args:
            actor: 実行者（admin であることは HTTP 層で確認済み）
            base_revision: 編集の基にした revision
            patch: 仕様書 §7.2 の編集可能パラメータのみを含む部分更新

        Returns:
            保存後の config（`meta.revision` が新しい値）

        Raises:
            ConfigNotFoundError: `config.json` が無い（404）
            ConfigRevisionConflictError: 楽観ロックの衝突（409）
            ConfigPatchError: 許可外パス・型/値域違反（422）
            ConfigValidationError: クロスフィールド制約の違反（422。T-05）
        """
        current = self._repo.load()
        if base_revision != current.meta.revision:
            raise ConfigRevisionConflictError(base_revision, current.meta.revision)

        candidate = apply_patch(current, patch)

        # ⚠️ 監査ログは**保存と同じトランザクション**に載せる。`save()` が
        # commit するので、ここで add しておけば config_revisions・config.json と
        # 一緒に確定する。保存が失敗したら下の except で rollback して消える。
        self._record_audit(
            actor=actor,
            revision=current.meta.revision + 1,
            diff=diff_configs(current, candidate),
        )
        try:
            return await self._repo.save(
                candidate, base_revision=base_revision, updated_by=actor.actor
            )
        except Exception:
            await self._db.rollback()
            raise

    def _record_audit(
        self, *, actor: Principal, revision: int, diff: dict[str, dict[str, Any]]
    ) -> None:
        """`config_update` を積む（commit は `save()`。設計書 §4.4）。

        （2026-08-14）T-10 の `AuditService` へ寄せた。**commit しないのは従来と
        同じ**で、`save()` のトランザクションに乗る＝ファイル書き込みが失敗したら
        監査ログも残らない。詳細な約束（秘密を書かない・握り潰さない）は
        `application/usecases/audit.py` のモジュール docstring を参照。
        """
        self._audit.record_config_update(
            actor=actor.actor,
            at=_now(),
            revision=revision,
            diff=diff,
            target=CONFIG_FILENAME,
        )


__all__ = [
    "EDITABLE_PATHS",
    "LIST_SELECTORS",
    "ConfigPatchError",
    "UpdateConfigUsecase",
    "apply_patch",
]
