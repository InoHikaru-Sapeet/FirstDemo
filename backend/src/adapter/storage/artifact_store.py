"""成果物ファイルの置き場を一元管理する。

このアプリは `config.json` / 中間xlsx / 生成HTML / `raw_articles.json` /
`validation_*.json` / `narrative_*.json` を「ファイルが正」として扱う
（設計書 §8・§13.4。最後の1つは 2026-08-16 の決定3＝T-44 で足したもの）。
成果物のパス解決・読み書きはすべてこの層を経由し、他のコードが直接 `open()`
しないようにする。将来クラウドストレージへ移す場合も、差し替えはここだけで済む。

満たす設計判断:

- **B（正規名は上書き＋履歴退避）**: 正規ファイルは固定名のまま upsert し、
  上書き前の内容を `_history/{period}/{revision}_{run_id}/` へ退避する。
  世代数は `history_max_generations` で頭打ちにする。
- **C（ドライランは隔離＋TTL）**: ドライランの出力は `scratch/dry-run/{id}/`
  にのみ書き、`scratch_ttl_hours` を過ぎたものを掃除する。
"""

import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from config import Settings, get_settings
from enterprise.entities import period as period_entity

ENCODING = "utf-8"

HISTORY_DIRNAME = "_history"
SCRATCH_DIRNAME = "scratch"
DRY_RUN_DIRNAME = "dry-run"

CONFIG_FILENAME = "config.json"
WEEKLY_REPORT_FILENAME = "weekly_ai_intelligence_report.xlsx"
MONTHLY_CASES_FILENAME = "monthly_ai_leading_cases.xlsx"

# 週次は ISO 週（2026-W31）、月次は年月（2026-07）。仕様書 §4・§8。
# ⚠️ **表記の定義は `enterprise.entities.period` が持つ**（T-21 で写しを寄せた）。
# ここが見るのは「ファイル名へ埋めてよい文字列か」までで、**実在する期間かどうかは
# 見ない**（`2026-W53` はパスとしては正当。実日付へ開けるかを確かめるのは
# `parse_period()` を呼ぶ crawl / filter の責務）。
WEEKLY_PERIOD_RE = period_entity.WEEKLY_PERIOD_RE
MONTHLY_PERIOD_RE = period_entity.MONTHLY_PERIOD_RE

# 生成 HTML の正規名（週刊は業界ごとに1通＝T-46 Step 4）。⚠️ **`weekly_html_path()` /
# `monthly_html_path()` と同じ形をここでも書いている。** 片方を変えたら
# もう片方も変えること（`tests/adapter/test_artifact_store.py` が一致を固定）。
WEEKLY_HTML_PREFIX = "weekly_ai_intelligence_newsletter_"
MONTHLY_HTML_PREFIX = "monthly_belief_"
WEEKLY_HTML_RE = re.compile(
    rf"^{re.escape(WEEKLY_HTML_PREFIX)}(?P<industry>.+)_(?P<period>\d{{4}}-W\d{{2}})\.html$"
)
MONTHLY_HTML_RE = re.compile(
    rf"^{re.escape(MONTHLY_HTML_PREFIX)}(?P<period>\d{{4}}-\d{{2}})\.html$"
)

# 期間に紐づかない配信対象（固定名の中間xlsx）。⚠️ `config.json` は入れない。
SERVABLE_FIXED_FILENAMES: frozenset[str] = frozenset(
    {WEEKLY_REPORT_FILENAME, MONTHLY_CASES_FILENAME}
)


class ArtifactStoreError(Exception):
    """成果物の置き場に関する不正な要求。"""


def _validate_segment(value: str, *, label: str) -> str:
    """ファイル名へ埋め込む値を検証する。

    period や industry は外部入力（API パラメータ・config 値）に由来しうるので、
    パス区切りや `..` が紛れ込むと `artifact_root` の外へ書けてしまう。

    Args:
        value: 検証する値
        label: エラーメッセージに出す項目名

    Returns:
        検証を通った値

    Raises:
        ArtifactStoreError: 空、またはパス区切り・`..`・NUL を含む場合
    """
    if not value:
        raise ArtifactStoreError(f"{label} が空です")
    if value != value.strip():
        raise ArtifactStoreError(f"{label} の前後に空白があります: {value!r}")
    if "/" in value or "\\" in value or "\x00" in value or value in (".", ".."):
        raise ArtifactStoreError(f"{label} にパス区切りを含められません: {value!r}")
    return value


def validate_period(period: str) -> str:
    """`2026-W31`（週次）または `2026-07`（月次）だけを受け付ける。"""
    _validate_segment(period, label="period")
    if not (WEEKLY_PERIOD_RE.match(period) or MONTHLY_PERIOD_RE.match(period)):
        raise ArtifactStoreError(
            f"period は 'YYYY-Www' または 'YYYY-MM' 形式が必要です: {period!r}"
        )
    return period


class ArtifactStore:
    """成果物ファイルの読み書きと世代管理。"""

    def __init__(
        self,
        root: Path,
        *,
        history_max_generations: int = 10,
        scratch_ttl_hours: int = 24,
        tz: tzinfo | None = None,
    ) -> None:
        self.root = root
        self.history_max_generations = history_max_generations
        self.scratch_ttl_hours = scratch_ttl_hours
        self._tz = tz

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ArtifactStore":
        settings = settings or get_settings()
        return cls(
            root=settings.artifact_root,
            history_max_generations=settings.history_max_generations,
            scratch_ttl_hours=settings.scratch_ttl_hours,
            tz=settings.tzinfo,
        )

    def _now(self) -> datetime:
        return datetime.now(tz=self._tz)

    # --- パス解決（正規名）------------------------------------------------

    @property
    def history_root(self) -> Path:
        return self.root / HISTORY_DIRNAME

    @property
    def scratch_root(self) -> Path:
        return self.root / SCRATCH_DIRNAME

    def config_path(self) -> Path:
        """判断基準ファイル `config.json`（設計書 §2.1・§8。T-11）。

        **期間に紐づかない唯一の正規成果物。** 読み書きは `ConfigRepository`
        （T-11）だけが行い、他は API 経由で参照する。

        ⚠️ **`archive()` による世代退避は使わない。** config の改訂履歴は DB
        （`config_revisions.config_snapshot`）が正で、`archive()` は period 単位の
        中間生成物向けだから（config に対応する period が無い）。
        """
        return self.root / CONFIG_FILENAME

    def weekly_report_path(self) -> Path:
        """週次の中間xlsx。ISO週ごとのシートを内部に持つ固定名ファイル。"""
        return self.root / WEEKLY_REPORT_FILENAME

    def monthly_cases_path(self) -> Path:
        """月次の中間xlsx。月ごとのシートを内部に持つ固定名ファイル。"""
        return self.root / MONTHLY_CASES_FILENAME

    def raw_articles_path(self, period: str) -> Path:
        return self.root / f"raw_articles_{validate_period(period)}.json"

    def validation_path(self, period: str) -> Path:
        return self.root / f"validation_{validate_period(period)}.json"

    def narrative_path(self, period: str) -> Path:
        """生成テキスト `narrative_{period}.json`（決定3 ／ T-44）。

        今週のポイント・示唆ボックス・巻頭言・章導入文・むすび（仕様書 §9.2・
        §10.2）の置き場。**中間xlsx の列（§8 の確定値）には入らない**ので、
        filter が書き render が読む受け渡しファイルとして独立している。

        ⚠️ **`archive()` による世代退避の対象**（設計判断B）。同じ period を
        再実行すると本文が丸ごと書き換わるため、「どの revision のどの実行が
        どの文章を出したか」が追えなくなる（HTML と同じ扱い）。
        """
        return self.root / f"narrative_{validate_period(period)}.json"

    def weekly_html_path(self, industry: str, period: str) -> Path:
        _validate_segment(industry, label="industry")
        validate_period(period)
        return self.root / f"{WEEKLY_HTML_PREFIX}{industry}_{period}.html"

    def monthly_html_path(self, period: str) -> Path:
        return self.root / f"{MONTHLY_HTML_PREFIX}{validate_period(period)}.html"

    def weekly_html_paths(self, period: str) -> list[Path]:
        """その週に**実際に出力された**週刊 HTML（業界ごとに1通。T-46 Step 4）。

        ⚠️ **config の `target_industries` からではなく、置いてあるファイルから
        数える。** 理由は2つ:

        1. `GET /reports/{period}`（T-27）は**全ロールが叩ける**が、config は
           admin 以外に**存在も中身も返さない**（仕様書 §2・§6.1）。config を
           見て一覧を作ると、対象業界の設定値を非 admin へ露出する経路になる。
        2. 過去の period を引いたときに、**その時点で出したもの**が並ぶ
           （設定を変えた後でも、出していない業界のリンクを並べない）。
        """
        validate_period(period)
        return sorted(self.root.glob(f"{WEEKLY_HTML_PREFIX}*_{period}.html"))

    # --- 配信できる成果物（T-27。生成物配信の許可リスト）------------------

    def is_servable(self, filename: str) -> bool:
        """`GET /files/{filename}` で外へ出してよいファイル名か。

        ⚠️ **許可リスト方式**（「危ないものを弾く」ではなく「これだけ通す」）。
        `artifact_root` には `config.json`（admin 限定・§6.1）・`raw_articles_*`・
        `validation_*`・`narrative_*`・`_history/`・`_runs/`・`scratch/`
        （ドライランの隔離出力・設計判断C）が同居している。**除外リスト方式に
        すると、新しい種類の成果物を足すたびに配信経路が黙って広がる。**

        通すのは §3.3 が `html_url` / `xlsx_url` として挙げているものだけ:

        - 週刊 HTML `weekly_ai_intelligence_newsletter_{industry}_{period}.html`
        - 月刊 HTML `monthly_belief_{period}.html`
        - 中間xlsx（週次・月次の固定名2つ）
        """
        try:
            _validate_segment(filename, label="filename")
        except ArtifactStoreError:
            return False
        if filename in SERVABLE_FIXED_FILENAMES:
            return True
        return bool(WEEKLY_HTML_RE.match(filename) or MONTHLY_HTML_RE.match(filename))

    def servable_path(self, filename: str) -> Path | None:
        """配信してよい実在のファイル。**それ以外は `None`**。

        ⚠️ 「配信対象外」と「存在しない」を**呼び出し元へ区別させない**
        （どちらも `None`）。区別できると、404 と 403 の差から
        `config.json` の有無を推定できてしまう（config ルーターと同じ理屈）。
        """
        if not self.is_servable(filename):
            return None
        path = self.root / filename
        return path if path.is_file() else None

    def dry_run_dir(self, dry_run_id: str) -> Path:
        """ドライランの隔離出力先。正規の成果物とは決して混ぜない（設計判断C）。"""
        _validate_segment(dry_run_id, label="dry_run_id")
        return self.scratch_root / DRY_RUN_DIRNAME / dry_run_id

    # --- 書き込み（原子的）------------------------------------------------

    @contextmanager
    def atomic_write(self, path: Path) -> Iterator[Path]:
        """一時ファイルへ書かせ、正常終了時だけ正規名へ差し替える。

        途中で失敗した成果物が「完成品」として読まれるのを防ぐ。
        xlsx のようにライブラリがパスを要求する場合もこれを使う。

        Args:
            path: 最終的な出力先

        Yields:
            書き込み先の一時ファイルパス
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            yield tmp_path
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def write_bytes(self, path: Path, data: bytes) -> None:
        with self.atomic_write(path) as tmp_path:
            tmp_path.write_bytes(data)

    def write_text(self, path: Path, text: str) -> None:
        """UTF-8 で書き出す（設計書 §14: 入出力はすべて UTF-8）。"""
        with self.atomic_write(path) as tmp_path:
            tmp_path.write_text(text, encoding=ENCODING)

    # --- 読み込み ---------------------------------------------------------

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding=ENCODING)

    def exists(self, path: Path) -> bool:
        return path.exists()

    # --- 履歴退避（設計判断B）--------------------------------------------

    def archive(
        self, path: Path, *, period: str, revision: int, run_id: str
    ) -> Path | None:
        """上書き前の正規ファイルを世代スナップショットとして退避する。

        config は admin の変更で結果が変わる前提なので（設計書 §3.2）、
        監査（§6.1・§14）には「どの revision のどの実行が何を出したか」が要る。
        一方 PROMPT-3 は固定名を入力にするため正規名は上書きせざるを得ない。
        両立させるのがこの退避。

        Args:
            path: 退避対象の正規ファイル
            period: 対象期間
            revision: 実行時に固定していた config の revision
            run_id: ジョブ実行ID

        Returns:
            退避先のパス。対象が存在しなければ None（初回実行）
        """
        if not path.exists():
            return None
        validate_period(period)
        _validate_segment(run_id, label="run_id")

        generation_dir = self.history_root / period / f"{revision}_{run_id}"
        generation_dir.mkdir(parents=True, exist_ok=True)
        destination = generation_dir / path.name
        shutil.copy2(path, destination)
        self.prune_history(period)
        return destination

    def prune_history(self, period: str) -> list[Path]:
        """世代数の上限を超えた古いスナップショットを削除する。

        Returns:
            削除した世代ディレクトリ
        """
        validate_period(period)
        period_dir = self.history_root / period
        if not period_dir.is_dir():
            return []

        generations = sorted(
            (d for d in period_dir.iterdir() if d.is_dir()),
            key=lambda d: (d.stat().st_mtime, d.name),
        )
        removed = generations[: max(0, len(generations) - self.history_max_generations)]
        for generation in removed:
            shutil.rmtree(generation)
        return removed

    # --- scratch の掃除（設計判断C）--------------------------------------

    def purge_expired_scratch(self) -> list[Path]:
        """TTL を過ぎたドライラン出力を削除する。

        Returns:
            削除したディレクトリ
        """
        dry_run_root = self.scratch_root / DRY_RUN_DIRNAME
        if not dry_run_root.is_dir():
            return []

        deadline = self._now() - timedelta(hours=self.scratch_ttl_hours)
        removed: list[Path] = []
        for entry in dry_run_root.iterdir():
            if not entry.is_dir():
                continue
            modified_at = datetime.fromtimestamp(entry.stat().st_mtime, tz=self._tz)
            if modified_at < deadline:
                shutil.rmtree(entry)
                removed.append(entry)
        return removed
