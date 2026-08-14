"""`config.json` の読み書き・revision 採番・改訂履歴（T-11。設計書 §4.3・§6.3）。

**ファイルが正。** `config.json` は `ArtifactStore` 経由のファイルとして保存し、
DB には**改訂履歴（`config_revisions`）だけ**を持つ（TASKS.md §1.1「永続化」）。
この切り分けは意図的で、DB に入れるのは監査ログ・config 改訂履歴・ユーザー／
セッションのみ。パイプライン（crawl → filter → render）は config をファイルとして
読む前提（設計書 §8）なので、正をファイルから動かさない。

⚠️ **現状はアプリ・DB・`config.json` がすべて同一ホスト（開発者の PC）にあるため、
事実上 config を編集できるのは1人だけ。** 楽観ロックは実装してあるが、競合が
実際に起きるのは共有ストレージへ移した後。AWS 移行時の論点は
[`docs/future-roadmap.md`](../../../docs/future-roadmap.md)「`config.json` の置き場」に
記録してある（切り替えは `ArtifactStore` の実装差し替えで行う想定）。

---

**設計上、動かしてはいけない点**

1. **書き込みは必ず検証を通す。** モデル検証（T-04）＋クロスフィールド検証（T-05）を
   通らない config は書かない。**値の自動補正はしない**（設計判断A）。
2. **`meta` はサーバが打つ。** `revision` / `updated_at` / `updated_by` は呼び出し元が
   送ってきた値を採用せず、必ずこの層で上書きする。偽装で revision を飛ばされると
   楽観ロックが意味を失う。
3. **書き込み順序は「履歴を flush → ファイルを書く → commit」。** ファイルが正なので、
   「履歴にはあるが実体が無い revision」を作らない（`get_pinned()` が実在しない
   config を返してしまう）。逆順は避ける。
4. **改訂履歴の一覧に config の中身を載せない。** `list_revisions()` は
   `config_snapshot` 列を**そもそも SELECT しない**。中身を返す経路は
   `get_pinned()` の1本だけにしておく（設計書 §3.3 の `items` は4項目のみ）。
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.models.config_revision import ConfigRevision
from adapter.storage.artifact_store import CONFIG_FILENAME, ArtifactStore
from config import Settings, get_settings
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.config_validation import ensure_valid_config
from enterprise.entities.json_document import (
    dump_json_document,
    parse_json_document,
    validate_json_data,
)

CONFIG_ADAPTER: TypeAdapter[IntelligenceConfig] = TypeAdapter(IntelligenceConfig)

# 初期投入時の revision（設計書 §10.3 手順6）。
INITIAL_REVISION = 1

# 差分要約に載せる最大件数。履歴一覧の1行に収まる長さで打ち切る。
DIFF_SUMMARY_MAX_ENTRIES = 5

# 差分から除く接頭辞。`meta.revision` / `updated_at` / `updated_by` は
# 保存のたびに必ず変わり、しかも `config_revisions` の列として別に持っている。
# 差分に混ぜると毎回3件が並び、**実際に変わった判断基準が埋もれる**。
DIFF_IGNORED_PREFIXES = ("meta.",)


class ConfigRepositoryError(Exception):
    """config の永続化に関する失敗の基底。"""


class ConfigNotFoundError(ConfigRepositoryError):
    """`config.json` が存在しない。

    初期マイグレーション（T-14）が未実行の状態。HTTP 層は 404 相当として扱う。
    """


class ConfigAlreadyExistsError(ConfigRepositoryError):
    """初期投入しようとしたが `config.json` が既にある。

    既存の判断基準を黙って上書きしないための関門（T-14 の冪等性。設計書 §10.4）。
    """


class ConfigRevisionConflictError(ConfigRepositoryError):
    """楽観ロックの衝突（設計書 §4.3・§6.3 ／ 仕様書 §6.3）。

    HTTP 層は `409 {"error":"revision_conflict","current_revision":N}` へ変換する
    （設計書 §3.3）。

    Attributes:
        base_revision: 呼び出し元が「これを基に編集した」と主張した revision
        current_revision: 実際の現行 revision
    """

    def __init__(self, base_revision: int, current_revision: int) -> None:
        self.base_revision = base_revision
        self.current_revision = current_revision
        super().__init__(
            f"別の更新が先に反映されています"
            f"（送信された base_revision={base_revision} / "
            f"現行 revision={current_revision}）。"
            "最新の内容を読み直してから、もう一度保存してください。"
        )


class ConfigRevisionNotFoundError(ConfigRepositoryError):
    """指定 revision のスナップショットが履歴に無い。

    Attributes:
        revision: 要求された revision
    """

    def __init__(self, revision: int) -> None:
        self.revision = revision
        super().__init__(
            f"revision={revision} の config スナップショットがありません。"
        )


class ConfigRevisionAlreadyRecordedError(ConfigRepositoryError):
    """採番しようとした revision の履歴行が既にある（ファイルと DB の不整合）。

    起きるのは「`config.json` を手で古い revision に戻した」「DB だけ復元した」等。
    そのまま insert すると主キー衝突の 500 になるので、原因の分かる形で止める。
    """

    def __init__(self, revision: int) -> None:
        self.revision = revision
        super().__init__(
            f"revision={revision} の改訂履歴が既に存在します。"
            f"{CONFIG_FILENAME} と config_revisions が食い違っています"
            "（ファイルを手で戻した場合など）。どちらを正にするかを決めてから再実行してください。"
        )


@dataclass(frozen=True)
class RevisionSummary:
    """`GET /config/history`（設計書 §3.3）が返す1件。

    **`config_snapshot` を持たない**のは意図的。履歴一覧から config の中身が
    漏れる経路を型として作らないため（§6.1「admin 以外に存在も中身も返さない」の
    裏返しで、admin 向けでも一覧に中身は要らない）。
    """

    revision: int
    updated_at: datetime
    updated_by: str | None
    diff_summary: str | None


# --- 差分（履歴の diff_summary ／ T-13・T-10 の監査ログ diff で共用）----------


def flatten_config(config: IntelligenceConfig) -> dict[str, Any]:
    """config をドット区切りパス → 値の平坦な辞書にする。

    パス表記は `ConfigIssue.path`（T-05）と同じ（`scoring_axes.0.weight`）。
    フロントが 422 の issues と差分を同じ方法でフィールドへ対応づけられる。

    **スカラーだけの配列は1つの値として扱う**（`enums.industry` /
    `scoring_axes.0.bands` 等）。要素ごとに展開すると、1件挿入しただけで
    以降の全要素が「変更」に見えてしまう。オブジェクトの配列は件数が固定
    （7 / 10 / 6 / 13。T-04）なので添字で展開して差し支えない。

    Args:
        config: 平坦化する config

    Returns:
        パス → 値。順序は宣言順（＝仕様書 §5.2 のキー順）で安定する
    """
    flat: dict[str, Any] = {}
    _flatten(config.model_dump(mode="json"), "", flat)
    return flat


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(item, f"{prefix}.{key}" if prefix else str(key), out)
        return
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        for index, item in enumerate(value):
            _flatten(item, f"{prefix}.{index}", out)
        return
    out[prefix] = value


def diff_configs(
    before: IntelligenceConfig, after: IntelligenceConfig
) -> dict[str, dict[str, Any]]:
    """2つの config の差分を `{path: {"before": x, "after": y}}` で返す。

    形は監査ログの `diff`（設計書 §4.4 の例
    `{"tunable_thresholds.min_total_score_to_publish": {"before": 60, "after": 62}}`）
    に合わせてある。T-10 / T-13 はこれをそのまま `audit_log.diff` へ入れられる。

    `meta.*` は除く（`DIFF_IGNORED_PREFIXES` の理由を参照）。

    Args:
        before: 変更前
        after: 変更後

    Returns:
        変更のあったパスだけを含む辞書。差分が無ければ空
    """
    old = flatten_config(before)
    new = flatten_config(after)

    diff: dict[str, dict[str, Any]] = {}
    for path in [*old, *(p for p in new if p not in old)]:
        if path.startswith(DIFF_IGNORED_PREFIXES):
            continue
        old_value = old.get(path)
        new_value = new.get(path)
        if old_value != new_value:
            diff[path] = {"before": old_value, "after": new_value}
    return diff


def summarize_diff(
    diff: dict[str, dict[str, Any]], *, max_entries: int = DIFF_SUMMARY_MAX_ENTRIES
) -> str | None:
    """差分を履歴一覧用の1行テキストにする。

    設計書 §3.3 の例は `"min_total_score_to_publish 60→62"` とセクション名を
    省いているが、ここでは**フルパスで書く**。`weight 25→30` では6軸のどれか
    分からず、履歴として役に立たないため。

    Args:
        diff: `diff_configs()` の結果
        max_entries: 並べる最大件数。超えた分は「他N件」に畳む

    Returns:
        要約テキスト。差分が無ければ None
    """
    if not diff:
        return None

    entries = [
        f"{path} {_render(change['before'])}→{_render(change['after'])}"
        for path, change in list(diff.items())[:max_entries]
    ]
    remainder = len(diff) - len(entries)
    if remainder > 0:
        entries.append(f"他{remainder}件")
    return " / ".join(entries)


def _render(value: Any) -> str:
    """差分要約に載せる値の表記。日本語をエスケープしない（設計書 §14: UTF-8）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --- リポジトリ ---------------------------------------------------------------


class ConfigRepository:
    """`config.json`（ファイルが正）と改訂履歴（DB）の読み書き。

    **認可はここではしない。** config ファミリが admin 限定であること
    （§6.2・§6.1）は HTTP 層の責務で、この層は「呼んでよいと確定した相手」から
    呼ばれる前提。`updated_by` の文字列も呼び出し元が決める
    （T-13 は `Principal.actor` 相当を渡す）。
    """

    def __init__(
        self,
        db: AsyncSession,
        store: ArtifactStore,
        *,
        tz: tzinfo = UTC,
    ) -> None:
        self._db = db
        self._store = store
        self._tz = tz

    @classmethod
    def from_settings(
        cls, db: AsyncSession, settings: Settings | None = None
    ) -> "ConfigRepository":
        settings = settings or get_settings()
        return cls(
            db,
            ArtifactStore.from_settings(settings),
            tz=settings.tzinfo,
        )

    def _now(self) -> datetime:
        """現在時刻（tz 付き）。テストが差し替えられるよう1箇所に集約する。

        tz を必ず持たせるのは、`config.meta.updated_at` を Asia/Tokyo で書き出す
        ため（設計書 §14）と、`UtcDateTime` が naive な datetime を拒否するため。
        """
        return datetime.now(tz=self._tz)

    # --- 参照 -------------------------------------------------------------

    @property
    def path(self) -> Path:
        """`config.json` の実体パス（`ArtifactStore` が解決したもの）。"""
        return self._store.config_path()

    def exists(self) -> bool:
        return self._store.exists(self.path)

    def load(self) -> IntelligenceConfig:
        """現行 config を読む。`revision` は `config.meta.revision`。

        検証は**モデル（構造・型・値域）まで**で、クロスフィールド検証（T-05）は
        通さない。理由は、手編集で合計100が崩れた config を読めなくすると
        `GET /config` が 500 になり、**admin が管理画面から直せなくなる**こと。
        書き込みは必ず `ensure_valid_config()` を通る（`save()`）ので、この経路を
        通って壊れた config が入ることはない。

        Returns:
            現行 config

        Raises:
            ConfigNotFoundError: `config.json` が無い（初期投入前）
            DocumentParseError: JSON が壊れている／スキーマに合わない
                （どのパスがなぜダメかを含む。T-06 の共通処理）
        """
        if not self.exists():
            raise ConfigNotFoundError(
                f"{self.path} がありません。"
                "初期マイグレーション（T-14）で config.json を作成してください。"
            )
        return parse_json_document(
            CONFIG_ADAPTER, self._store.read_text(self.path), label=CONFIG_FILENAME
        )

    async def get_pinned(self, revision: int) -> IntelligenceConfig:
        """指定 revision のスナップショットを DB から読む（設計書 §6.3・§14）。

        ジョブは開始時点の revision を1回だけ決め、以降はこれで固定参照する。
        実行中に admin が config を保存しても**途中で基準が切り替わらない**＝
        再現性要件（§14）そのもの。

        ⚠️ 正はあくまでファイル。ここが返すのは**保存時に取ったコピー**なので、
        `config.json` を手で書き換えた場合は一致しない（手編集を追跡する設計に
        なっていない。共有ストレージへ移すときの論点は future-roadmap.md）。

        Args:
            revision: 固定したい revision

        Raises:
            ConfigRevisionNotFoundError: その revision の履歴が無い
            DocumentParseError: スナップショットが現行スキーマに合わない
        """
        row = await self._db.get(ConfigRevision, revision)
        if row is None:
            raise ConfigRevisionNotFoundError(revision)
        return validate_json_data(
            CONFIG_ADAPTER,
            row.config_snapshot,
            label=f"config_revisions[revision={revision}]",
        )

    async def list_revisions(
        self, *, limit: int | None = None
    ) -> list[RevisionSummary]:
        """改訂履歴を新しい順に返す（`GET /config/history`。設計書 §3.3）。

        `config_snapshot` 列は **SELECT しない**（一覧に config の中身を混ぜない。
        毎回 config 全体を読む無駄も避ける）。

        Args:
            limit: 最大件数。None なら全件

        Returns:
            revision の降順（新しいものが先頭）
        """
        statement = (
            select(
                ConfigRevision.revision,
                ConfigRevision.updated_at,
                ConfigRevision.updated_by,
                ConfigRevision.diff_summary,
            )
            .order_by(ConfigRevision.revision.desc())
            .limit(limit)
        )
        rows = (await self._db.execute(statement)).all()
        return [
            RevisionSummary(
                revision=row.revision,
                updated_at=row.updated_at,
                updated_by=row.updated_by,
                diff_summary=row.diff_summary,
            )
            for row in rows
        ]

    # --- 書き込み ---------------------------------------------------------

    async def create_initial(
        self, config: IntelligenceConfig, *, updated_by: str | None = None
    ) -> IntelligenceConfig:
        """初期 config を作る（移行 CLI 用。設計書 §10.3 手順6）。

        `revision=1` / `updated_at=現在時刻` を打つ。`updated_by` の既定が None
        なのは、初期投入は人の編集ではないため（§10.3 手順6 が `updated_by=null`）。

        Args:
            config: 投入する config（`meta` の値は上書きされる）
            updated_by: 実行者。移行 CLI は指定しない

        Returns:
            実際に書き込まれた config（`meta` が確定した状態）

        Raises:
            ConfigAlreadyExistsError: `config.json` が既にある
            ConfigValidationError: クロスフィールド制約に違反している（T-05）
            ConfigRevisionAlreadyRecordedError: revision=1 の履歴行が既にある
        """
        if self.exists():
            raise ConfigAlreadyExistsError(
                f"{self.path} が既に存在します。"
                "既存の判断基準を上書きしないため、初期投入は行いません。"
            )
        return await self._write(
            config,
            revision=INITIAL_REVISION,
            updated_by=updated_by,
            diff_summary=None,
        )

    async def save(
        self,
        config: IntelligenceConfig,
        *,
        base_revision: int,
        updated_by: str,
    ) -> IntelligenceConfig:
        """検証を通してから上書き保存し、`revision` を1つ進める（設計書 §4.3）。

        手順は §1.3 のシーケンスどおり: 検証 → 楽観ロック比較 → 書き込み＋
        `revision++` / `updated_at` / `updated_by` → 履歴記録。

        ⚠️ **`config.meta` に何を入れて渡しても無視する。** revision は現行＋1、
        `updated_at` はサーバ時刻、`updated_by` は引数の値で上書きする。
        呼び出し元に revision を決めさせると楽観ロックが成立しない。

        Args:
            config: 保存したい内容（`meta` は上書きされる）
            base_revision: 呼び出し元が編集の基にした revision（楽観ロック）
            updated_by: 実行者（T-13 は `Principal.actor` 相当を渡す）

        Returns:
            保存後の config（`meta.revision` が新しい値になっている）

        Raises:
            ConfigNotFoundError: `config.json` が無い（初期投入前）
            ConfigRevisionConflictError: `base_revision` が現行と一致しない（409）
            ConfigValidationError: クロスフィールド制約に違反している（422。T-05）
            ConfigRevisionAlreadyRecordedError: 採番先の履歴行が既にある
        """
        current = self.load()
        if base_revision != current.meta.revision:
            raise ConfigRevisionConflictError(base_revision, current.meta.revision)

        # 検証は書き込みの前。**ここで値を補正しない**（設計判断A）。
        ensure_valid_config(config)

        stamped = self._stamp(
            config, revision=current.meta.revision + 1, updated_by=updated_by
        )
        return await self._write(
            stamped,
            revision=stamped.meta.revision,
            updated_by=updated_by,
            diff_summary=summarize_diff(diff_configs(current, stamped)),
            already_stamped=True,
        )

    # --- 内部 -------------------------------------------------------------

    def _stamp(
        self,
        config: IntelligenceConfig,
        *,
        revision: int,
        updated_by: str | None,
    ) -> IntelligenceConfig:
        """`meta` の revision / updated_at / updated_by をサーバ側の値で確定する。"""
        meta = config.meta.model_copy(
            update={
                "revision": revision,
                "updated_at": self._now(),
                "updated_by": updated_by,
            }
        )
        return config.model_copy(update={"meta": meta})

    async def _write(
        self,
        config: IntelligenceConfig,
        *,
        revision: int,
        updated_by: str | None,
        diff_summary: str | None,
        already_stamped: bool = False,
    ) -> IntelligenceConfig:
        """検証済み config をファイルへ書き、改訂履歴を記録する。

        **順序が要点**: 履歴行を flush（＝主キー衝突をここで検出）→ ファイルを
        原子的に書く（T-02）→ commit。ファイル書き込みが失敗したら rollback して
        履歴を残さない。ファイルが正なので「履歴にはあるが実体が無い revision」を
        作らない方を優先する（`get_pinned()` が実在しない config を返すのを防ぐ）。
        """
        if not already_stamped:
            ensure_valid_config(config)
            config = self._stamp(config, revision=revision, updated_by=updated_by)

        await self._ensure_revision_is_free(revision)

        self._db.add(
            ConfigRevision(
                revision=revision,
                updated_at=config.meta.updated_at,
                updated_by=updated_by,
                config_snapshot=config.model_dump(mode="json"),
                diff_summary=diff_summary,
            )
        )
        await self._db.flush()

        try:
            self._store.write_text(
                self.path, dump_json_document(CONFIG_ADAPTER, config)
            )
        except Exception:
            await self._db.rollback()
            raise

        await self._db.commit()
        return config

    async def _ensure_revision_is_free(self, revision: int) -> None:
        if await self._db.get(ConfigRevision, revision) is not None:
            raise ConfigRevisionAlreadyRecordedError(revision)


__all__ = [
    "CONFIG_ADAPTER",
    "INITIAL_REVISION",
    "ConfigAlreadyExistsError",
    "ConfigNotFoundError",
    "ConfigRepository",
    "ConfigRepositoryError",
    "ConfigRevisionAlreadyRecordedError",
    "ConfigRevisionConflictError",
    "ConfigRevisionNotFoundError",
    "RevisionSummary",
    "diff_configs",
    "flatten_config",
    "summarize_diff",
]
