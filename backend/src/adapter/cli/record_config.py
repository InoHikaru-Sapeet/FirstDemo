"""手編集した `config.json` を改訂履歴へ記録する（T-47。設計書 §4.3・§6.3）。

    make config-record                    # dry（既定）: 差分と新 revision の予告だけ
    make config-record ARGS="--apply"     # 検証を通ったら記録する

**なぜ要るか。** `config.json` は「ファイルが正」（T-11）で、サーバを立てずに
手で調整する運用が正式に残っている。一方で実行時の判断基準は
`config_revisions` のスナップショットを固定参照する（§6.3・T-26 の `get_pinned`）。
ファイルだけ直すと両者が食い違い、**実行が `ConfigPinError` で止まる**
（2026-08-17 に実運用で発生した。TASKS.md T-47 の背景）。このコマンドは
「ファイルの現状を新しい revision として履歴と監査ログへ追いつかせる」1本で、
`PUT /config` の後始末に相当する。

---

**動かしてはいけない点**

1. **dry が既定。** `--apply` を明示しない限り、ファイルも DB も触らない。
2. **検証に落ちたら書かない。** モデル検証（T-04）＋クロスフィールド検証（T-05）を
   通らない config は記録しない。**値を補正して通すことはしない**（設計判断A）。
   ⚠️ ここで通してしまうと、次の実行が壊れた基準を**固定参照して**走る。
3. **書き込みは `UpdateConfigUsecase.record_current()`（T-13）経由**。ここで
   `save()` も `open()` も直接呼ばない。原子的書き込み・revision 採番・
   `config_revisions`・監査ログ `config_update` は `PUT /config` と同じ1本を通す
   （経路を増やすと、管理画面から保存したときと CLI から記録したときで
   履歴の形が食い違う）。
4. **差分の基準は DB の最新スナップショット**であって、ファイルではない。
   ファイルは既に編集後なので、ファイル基準では差分が必ず空になる。
5. **差分が無ければ何もしない**（冪等）。2回続けて `--apply` しても
   revision は1つしか増えない。

---

**「差分」の定義（このコマンドが記録する条件）**

- **内容**：ファイルの中身と、DB 最新 revision のスナップショットの差（`meta.*` は除く）
- **指し先**：ファイルの `meta.revision` が DB 最新と一致しているか

⚠️ **後者も記録の対象にしている。** 中身が最新スナップショットと同じでも
`meta.revision` が古い値を指していると、`get_pinned()` は**別の（古い）
スナップショット**を固定してしまう。記録後は
「`config.json` の `meta.revision` == DB 最新 revision」かつ
「その revision のスナップショット == ファイルの中身」が成り立つ——これが
実行前の固定（§6.3）が正しく効くための条件そのもの。

⚠️ **採番は「ファイルの `meta.revision` + 1」**（`ConfigRepository.save()` の
規則そのまま。revision の正はファイル）。両者が揃っている通常の状態では
「履歴の最新 + 1」と同じ値になる。**ファイルの revision のほうが古い**という
食い違い方をしている場合、採番先の履歴行が既にあるので
`ConfigRevisionAlreadyRecordedError` で**拒否する**（黙って上書きも飛び番も
しない。どちらを正にするかは人が決める）。
"""

import asyncio
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapter.config_repository import (
    ConfigRepository,
    ConfigRepositoryError,
    ConfigRevisionConflictError,
    diff_config_data,
)
from adapter.database.database import db_manager
from adapter.storage.artifact_store import CONFIG_FILENAME
from application.usecases.update_config import UpdateConfigUsecase
from config import Settings, get_settings
from enterprise.entities.config_validation import ConfigIssue, validate_config
from enterprise.entities.json_document import DocumentIssue, DocumentParseError

# 監査ログの `actor` と `updated_by`（設計書 §4.4 の `role:subject` 形式）。
# 人ではなくコマンドが行為者なので、ロールの位置に `cli` を置く
# （T-41 の `cli:create-admin` と同じ形）。⚠️ **`admin:` を騙らない**——
# 誰の操作か分からない変更を、管理画面からの保存に見せかけないため。
CLI_ACTOR = "cli:config-record"

EXIT_OK = 0
# 記録できなかった（履歴とファイルが食い違っていて、どちらを正にするか人が決める）。
EXIT_REFUSED = 1
# 検証に失敗した（書き込んでいない）。
EXIT_VALIDATION_FAILED = 2
# `config.json` が無い・JSON として読めない。
EXIT_INVALID_INPUT = 3

# 差分の表示件数の上限。全件出すと 100 行を超えることがあるので畳む
# （`diff_summary` の 5 件より多めにするのは、CLI は画面で読むため）。
DIFF_PREVIEW_MAX_ENTRIES = 20


@dataclass
class RecordReport:
    """1回の実行の結果（画面に出す唯一の成果物）。"""

    config_path: Path
    apply: bool
    file_revision: int | None = None
    latest_revision: int | None = None
    parse_issues: list[DocumentIssue] = field(default_factory=list)
    issues: list[ConfigIssue] = field(default_factory=list)
    diff: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_revision: int | None = None
    recorded_revision: int | None = None
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return not self.parse_issues and not self.issues

    @property
    def revision_pointer_is_stale(self) -> bool:
        """ファイルの `meta.revision` が DB 最新を指していない。

        中身が同じでも、ここがずれていると `get_pinned()` が別の revision を
        固定する（モジュール docstring の「差分の定義」）。
        """
        return (
            self.file_revision is not None
            and self.latest_revision is not None
            and self.file_revision != self.latest_revision
        )

    @property
    def needs_record(self) -> bool:
        return bool(self.diff) or self.revision_pointer_is_stale


def render_report(report: RecordReport) -> str:
    """レポートを人が読む形に整える。"""
    mode = "apply（記録する）" if report.apply else "dry（既定・記録しない）"
    lines = [
        "=== config.json を改訂履歴へ記録（T-47 / 設計書 §6.3）===",
        f"モード      : {mode}",
        f"対象 config : {report.config_path}",
    ]

    if report.file_revision is not None:
        latest = (
            "なし（履歴が空）"
            if report.latest_revision is None
            else str(report.latest_revision)
        )
        lines += [
            "",
            f"ファイルの revision : {report.file_revision}",
            f"履歴の最新 revision : {latest}",
        ]

    if report.parse_issues:
        lines += ["", f"■ 構造検証エラー {len(report.parse_issues)}件（T-04）"]
        lines += [f"  - {i.path}: {i.reason}" for i in report.parse_issues]

    if report.issues:
        lines += ["", f"■ 検証エラー {len(report.issues)}件（T-05）"]
        lines += [f"  - [{i.code}] {i.path}: {i.reason}" for i in report.issues]

    if report.ok and report.file_revision is not None:
        lines += ["", _render_diff(report)]
        if (warning := _render_pointer_warning(report)) is not None:
            lines += ["", warning]

    lines += ["", _render_outcome(report)]
    return "\n".join(lines)


def _render_pointer_warning(report: RecordReport) -> str | None:
    """`meta.revision` が履歴の最新を指していないことの説明。

    中身の差分と**別に出す**。差分が無くてもここがずれていれば実行時の固定
    （§6.3）は古いスナップショットを選ぶし、逆に差分があるときは
    「なぜ採番がその番号になるのか」の説明が要る。
    """
    if not report.revision_pointer_is_stale:
        return None
    lines = [
        f"■ ⚠️ ファイルの meta.revision（{report.file_revision}）が"
        f"履歴の最新（{report.latest_revision}）を指していない",
        "  このままでは実行時の固定（§6.3）が別のスナップショットを選ぶ。",
    ]
    if (
        report.latest_revision is not None
        and report.file_revision is not None
        and report.file_revision < report.latest_revision
    ):
        lines.append(
            f"  採番先の revision={report.next_revision} は履歴に既にある可能性が"
            "高く、その場合は記録を拒否する（どちらを正にするかは人が決める）。"
        )
    return "\n".join(lines)


def _render_diff(report: RecordReport) -> str:
    if not report.diff:
        return "■ 差分なし（履歴の最新スナップショットと一致）"

    shown = list(report.diff.items())[:DIFF_PREVIEW_MAX_ENTRIES]
    lines = [f"■ 差分 {len(report.diff)}件（meta.* を除く）"]
    lines += [
        f"  - {path}: {change['before']!r} → {change['after']!r}"
        for path, change in shown
    ]
    if (remainder := len(report.diff) - len(shown)) > 0:
        lines.append(f"  - 他{remainder}件")
    return "\n".join(lines)


def _render_outcome(report: RecordReport) -> str:
    if report.refusal is not None:
        return f"→ 記録できませんでした: {report.refusal}"
    if not report.ok:
        return (
            "→ 検証に失敗したため記録していません（ファイル・DB とも無変更）。"
            "config.json を直してから、もう一度実行してください。"
        )
    if report.recorded_revision is not None:
        return (
            f"→ revision={report.recorded_revision} として記録しました"
            f"（{CONFIG_FILENAME} の meta.revision も更新済み / "
            f"updated_by={CLI_ACTOR}）。"
        )
    if not report.needs_record:
        return "→ 記録済み・変更なし（何もしていません）。"
    return (
        f"→ dry モードなので記録していません。"
        f"記録すると revision={report.next_revision} になります。"
        "実行するには make config-record ARGS='--apply' を付けてください。"
    )


async def run(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    *,
    apply: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    """CLI の本体。終了コードを返す（例外で落ちない）。

    Args:
        repo: config の読み書き口（T-11）
        usecase: `PUT /config` と同じユースケース（T-13）。**書き込みはここ経由**
        apply: True なら検証を通ったときだけ記録する。False（既定）は dry
        out: 出力先（テストで差し替える）

    Returns:
        `EXIT_OK` / `EXIT_REFUSED` / `EXIT_VALIDATION_FAILED` / `EXIT_INVALID_INPUT`
    """
    report = RecordReport(config_path=repo.path, apply=apply)

    # 手順1: ファイルを読む（モデル検証まで＝T-04）。
    try:
        config = repo.load()
    except DocumentParseError as exc:
        report.parse_issues = exc.issues
        out(render_report(report))
        return EXIT_VALIDATION_FAILED
    except ConfigRepositoryError as exc:
        out(f"中止しました: {exc}")
        return EXIT_INVALID_INPUT

    report.file_revision = config.meta.revision

    # 手順2: クロスフィールド検証（T-05）。⚠️ 通らないものは記録しない。
    report.issues = validate_config(config)
    if not report.ok:
        out(render_report(report))
        return EXIT_VALIDATION_FAILED

    # 手順3: DB の最新 revision と比べる。履歴が空なら「全部が新規」。
    latest = await repo.list_revisions(limit=1)
    baseline: dict[str, Any] = {}
    if latest:
        report.latest_revision = latest[0].revision
        baseline = await repo.get_snapshot_data(latest[0].revision)

    report.diff = diff_config_data(baseline, config.model_dump(mode="json"))
    report.next_revision = config.meta.revision + 1

    if not report.needs_record:
        out(render_report(report))
        return EXIT_OK

    if not apply:
        out(render_report(report))
        return EXIT_OK

    # 手順4: 記録（`PUT /config` と同じ経路。原子的書き込み・履歴・監査ログ）。
    try:
        recorded = await usecase.record_current(CLI_ACTOR, diff_base_data=baseline)
    except ConfigRevisionConflictError as exc:
        # 読んでから記録するまでに管理画面から保存された場合。
        report.refusal = str(exc)
        out(render_report(report))
        return EXIT_REFUSED
    except ConfigRepositoryError as exc:
        # 採番先の履歴行が既にある等（ファイルを手で古い revision へ戻した場合）。
        report.refusal = str(exc)
        out(render_report(report))
        return EXIT_REFUSED

    report.recorded_revision = recorded.meta.revision
    out(render_report(report))
    return EXIT_OK


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="config-record",
        description=(
            "手編集した config.json を改訂履歴・監査ログへ記録する"
            "（T-47 / 設計書 §6.3）。既定は dry で、--apply を付けたときだけ記録する。"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="検証を通ったら記録する（既定は dry で何も書かない）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args: Namespace = _build_parser().parse_args(argv)

    async def _run() -> int:
        settings: Settings = get_settings()
        async with db_manager.session() as db:
            repo = ConfigRepository.from_settings(db, settings)
            usecase = UpdateConfigUsecase(db=db, repo=repo)
            return await run(repo, usecase, apply=args.apply)

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("中止しました: 中断されました。")
        return EXIT_INVALID_INPUT


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLI_ACTOR",
    "EXIT_INVALID_INPUT",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_VALIDATION_FAILED",
    "RecordReport",
    "main",
    "render_report",
    "run",
]
