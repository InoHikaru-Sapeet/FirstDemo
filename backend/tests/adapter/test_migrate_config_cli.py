"""xlsx → config.json 初期マイグレーション CLI（T-14。設計書 §10）。

重点は「破ると判断基準が黙って書き換わる／確定値と食い違う」性質:

- **生成結果が仕様書 §5.2 の実データと完全一致する**（件数 7/10/6/13・ID・配点・
  初期しきい値。`tests/enterprise/data/config_initial.json` が §5.2 の逐語コピー）
- **日本語 → ID の正規化**（`中〜高` → `mid_high`。仕様書 §5.3）
- **dry が既定**で、既存 config は上書きしない（revision を維持して差分だけ）
- **検証（手順4-5）が落ちたら書かない**
- **書き込みは `ConfigRepository` 経由**（改訂履歴が残る＝直接 open() していない）
- xlsx と §5.2 の**文言差分は警告として必ず報告する**（黙って寄せない）

入力 xlsx は実ファイル（`docs/source/weekly_ai_intelligence_requirements.xlsx`）。
異常系は実ファイルを tmp へ写してセルを壊したものを使う。
"""

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapter.cli.migrate_config import (
    DEFAULT_XLSX,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_VALIDATION_FAILED,
    SPEC_TEXT_NORMALIZATIONS,
    build_config_payload,
    run,
)
from adapter.config_repository import CONFIG_ADAPTER, ConfigRepository
from adapter.database.base import Base
from adapter.database.models.config_revision import ConfigRevision
from adapter.storage.artifact_store import ArtifactStore
from enterprise.entities.config import (
    INFORMATION_CATEGORY_IDS,
    REQUIRED_TAG_IDS,
    SCORING_AXIS_IDS,
)

# §5.2 の逐語コピー（T-04 / T-05 / T-11 のテストと同じ実データ）。
SPEC_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

JST = ZoneInfo("Asia/Tokyo")
# §5.2 の `meta.updated_at`。実データと1バイトも違わない比較をするために揃える。
SPEC_UPDATED_AT = datetime.fromisoformat("2026-08-12T00:00:00+09:00")


@pytest.fixture(scope="session")
def spec_config() -> dict[str, Any]:
    return json.loads(SPEC_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts", tz=JST)


@pytest.fixture
def repo(db: AsyncSession, store: ArtifactStore) -> ConfigRepository:
    return ConfigRepository(db, store, tz=JST)


class Output:
    """CLI の出力を溜める（レポートの内容を検査するため）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def out() -> Output:
    return Output()


@pytest.fixture
def edit_xlsx(tmp_path: Path) -> Callable[[Callable[[Any], None]], Path]:
    """実 xlsx を写してセルを書き換えたものを作る（異常系の入力）。

    `data_only=True` で開くのは、原本の「合計」セルが `=SUM(C5:C10)` の数式で、
    そのまま保存すると計算結果が失われるため（値として写してから壊す）。
    """

    def _edit(mutate: Callable[[Any], None]) -> Path:
        workbook = load_workbook(DEFAULT_XLSX, data_only=True)
        mutate(workbook)
        broken = tmp_path / "broken.xlsx"
        workbook.save(broken)
        return broken

    return _edit


def read_config(repo: ConfigRepository) -> dict[str, Any]:
    return json.loads(repo.path.read_text(encoding="utf-8"))


# --- 手順1〜3・5: xlsx から §5.2 が再現できる --------------------------------


def test_the_generated_config_matches_the_spec_data(
    spec_config: dict[str, Any],
) -> None:
    """xlsx から起こした config が仕様書 §5.2 の実データと**完全一致**する。

    これが T-14 の受け入れ条件そのもの（設計書 §10.3 手順5）。キー順まで比較する
    のは、`config.json` の diff が読める形（§5.2 のキー順）を保つため。
    """
    payload, _, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )
    config = CONFIG_ADAPTER.validate_python(payload)
    generated = config.model_dump(mode="json")

    assert generated == spec_config
    assert list(generated) == list(spec_config)


def test_the_counts_are_the_confirmed_numbers() -> None:
    """7カテゴリ / 10タグ / 6軸 / 13除外ルール（仕様書 §5.1・手順5）。"""
    payload, _, declared_total = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )

    assert len(payload["information_categories"]) == 7
    assert len(payload["required_tags"]) == 10
    assert len(payload["scoring_axes"]) == 6
    assert len(payload["exclusion_rules"]) == 13
    # xlsx の「合計」行も 100 を主張している
    assert declared_total == 100
    assert sum(axis["weight"] for axis in payload["scoring_axes"]) == 100


def test_japanese_labels_become_the_fixed_ids() -> None:
    """日本語 → ID の正規化（手順2）。ID は §5.2 の確定値で、順序も保つ。

    ID は機械的な変換では作れない（「業務領域」→ `business_area`）ので対応表で
    引いている。表が壊れればここで落ちる。
    """
    payload, _, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )

    assert tuple(c["id"] for c in payload["information_categories"]) == (
        INFORMATION_CATEGORY_IDS
    )
    assert tuple(t["id"] for t in payload["required_tags"]) == REQUIRED_TAG_IDS
    assert tuple(a["id"] for a in payload["scoring_axes"]) == SCORING_AXIS_IDS


def test_the_priority_written_as_mid_high_in_japanese_is_normalized() -> None:
    """「中〜高」→ `mid_high`（仕様書 §5.3）。取り違えると手順5で落ちる。"""
    payload, _, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )
    priorities = {c["id"]: c["priority"] for c in payload["information_categories"]}

    assert priorities["ai_major_company_model"] == "mid_high"
    assert priorities["ai_agent_automation"] == "high"
    assert priorities["ai_implementation_ops"] == "mid"


def test_the_exclusion_severities_are_normalized_to_the_five_values() -> None:
    """除外区分の日本語 → §5.4 の5値。`enabled` は初期値どおり全件 true。"""
    payload, _, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )
    severities = {r["no"]: r["severity"] for r in payload["exclusion_rules"]}

    assert severities[1] == "full_exclude"
    assert severities[3] == "default_exclude"
    assert severities[11] == "low_priority"
    assert severities[12] == "merge"
    assert severities[13] == "low_priority_or_exclude"
    assert all(rule["enabled"] for rule in payload["exclusion_rules"])


def test_the_score_bands_are_split_without_breaking_the_slash_inside_a_band() -> None:
    """得点帯は ` / ` で割る。`9-10:公式/政府一次情報` を割ってしまわない。"""
    payload, _, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )
    bands = {axis["id"]: axis["bands"] for axis in payload["scoring_axes"]}

    assert bands["reliability"][0] == "9-10:公式/政府一次情報"
    assert len(bands["reliability"]) == 5
    assert len(bands["urgency_freshness"]) == 4


def test_the_thresholds_come_from_the_spec_initial_values() -> None:
    """`tunable_thresholds` は xlsx に無く §5.2 の初期値を投入する（手順3）。"""
    payload, _, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )
    thresholds = payload["tunable_thresholds"]

    assert thresholds["min_total_score_to_publish"] == 60
    assert thresholds["adoption_class_score_map"] == {
        "propose_next_meeting": 85,
        "reference_info": 70,
        "share_only": 60,
    }
    assert thresholds["min_reliability_score_to_publish"] == 5
    assert thresholds["weekly"]["target_industries"] == ["不動産"]
    assert thresholds["monthly"]["target_case_count"] == 15
    assert thresholds["dedup"]["title_similarity_threshold"] == 0.85


# --- 文言差分は黙って寄せない（2026-08-14 決定 / 要確認事項 #9）--------------


def test_the_text_divergences_from_the_spec_are_reported_as_warnings() -> None:
    """§5.2 へ寄せた7箇所は必ず警告に出す。

    黙って書き換えると、xlsx を見た人が `config.json` の文言を説明できなくなる。
    未適用の正規化行が無いことも確かめる（表に腐った行を残さない）。
    """
    _, warnings, _ = build_config_payload(
        DEFAULT_XLSX, revision=1, updated_at=SPEC_UPDATED_AT
    )

    assert len(warnings) == len(SPEC_TEXT_NORMALIZATIONS) == 7
    paths = [warning.path for warning in warnings]
    assert paths == [
        "required_tags.0.purpose",
        "required_tags.1.purpose",
        "required_tags.3.purpose",
        "required_tags.5.purpose",
        "required_tags.8.purpose",
        "required_tags.9.purpose",
        "scoring_axes.4.bands",
    ]
    assert all("§5.2" in warning.reason for warning in warnings)


def test_a_normalization_that_no_longer_applies_is_reported(
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """xlsx 側が §5.2 に揃えられたら、その正規化行は「未適用」として報告される。

    表の行が発火しなくなったことに気づけないと、要らない行が残り続ける。
    """

    def fix_purpose(workbook: Any) -> None:
        workbook["必須タグ"]["D5"] = "レポート全体の分類軸"

    _, warnings, _ = build_config_payload(
        edit_xlsx(fix_purpose), revision=1, updated_at=SPEC_UPDATED_AT
    )

    unused = [w for w in warnings if w.path == "SPEC_TEXT_NORMALIZATIONS"]
    assert len(unused) == 1
    assert "レポート全体の分類軸になる" in unused[0].reason
    assert "未適用" in unused[0].reason


# --- dry が既定（設計書 §10.4）------------------------------------------------


async def test_dry_is_the_default_and_writes_nothing(
    repo: ConfigRepository, out: Output
) -> None:
    """`--apply` を付けない限り何も書かない。"""
    exit_code = await run(repo, out=out)

    assert exit_code == EXIT_OK
    assert not repo.exists()
    assert "dry" in out.text
    assert "--apply" in out.text


async def test_dry_does_not_record_a_revision(
    repo: ConfigRepository, db: AsyncSession, out: Output
) -> None:
    """dry は DB も触らない（改訂履歴に revision=1 を作らない）。"""
    await run(repo, out=out)

    assert (await db.execute(select(ConfigRevision.revision))).scalars().all() == []


# --- 手順6: 書き込み（apply）--------------------------------------------------


async def test_apply_writes_revision_1_with_no_updated_by(
    repo: ConfigRepository, out: Output, spec_config: dict[str, Any]
) -> None:
    """`meta.revision=1` / `updated_by=null` / `updated_at=migration時刻`（手順6）。

    内容は §5.2 と一致する（`meta.updated_at` だけは実行時刻）。
    """
    before = datetime.now(tz=JST)
    exit_code = await run(repo, apply=True, out=out)
    after = datetime.now(tz=JST)

    assert exit_code == EXIT_OK
    written = read_config(repo)
    assert written["meta"]["revision"] == 1
    assert written["meta"]["updated_by"] is None
    assert before <= datetime.fromisoformat(written["meta"]["updated_at"]) <= after

    ignore_updated_at = {"updated_at"}
    assert {
        key: value
        for key, value in written["meta"].items()
        if key not in ignore_updated_at
    } == {
        key: value
        for key, value in spec_config["meta"].items()
        if key not in ignore_updated_at
    }
    for section in ("information_categories", "required_tags", "scoring_axes"):
        assert written[section] == spec_config[section]
    assert written["exclusion_rules"] == spec_config["exclusion_rules"]
    assert written["enums"] == spec_config["enums"]
    assert written["tunable_thresholds"] == spec_config["tunable_thresholds"]


async def test_apply_goes_through_the_repository_and_records_the_revision(
    repo: ConfigRepository, db: AsyncSession, out: Output
) -> None:
    """書き込みは `ConfigRepository.create_initial()` 経由（直接 open() しない）。

    直接ファイルを書いていたら改訂履歴（`config_revisions`）が空になる。
    """
    await run(repo, apply=True, out=out)

    rows = (await db.execute(select(ConfigRevision))).scalars().all()
    assert [row.revision for row in rows] == [1]
    assert rows[0].updated_by is None
    assert rows[0].config_snapshot["meta"]["revision"] == 1


# --- 冪等性（設計書 §10.4）---------------------------------------------------


async def test_an_existing_config_is_never_overwritten(
    repo: ConfigRepository, out: Output
) -> None:
    """2回目の `--apply` は拒否する。既存の判断基準を黙って上書きしない。"""
    assert await run(repo, apply=True, out=out) == EXIT_OK
    first = read_config(repo)

    assert await run(repo, apply=True, out=out) == EXIT_REFUSED
    assert read_config(repo) == first


async def test_dry_keeps_the_existing_revision_and_reports_the_diff(
    repo: ConfigRepository, out: Output
) -> None:
    """既存 config があれば revision を維持して差分だけ出す（再実行可能）。"""
    await run(repo, apply=True, out=out)
    current = repo.load()
    edited = current.model_copy(
        update={
            "tunable_thresholds": current.tunable_thresholds.model_copy(
                update={"min_total_score_to_publish": 58}
            )
        }
    )
    await repo.save(edited, base_revision=1, updated_by="admin:usr_1")

    report = Output()
    exit_code = await run(repo, out=report)

    assert exit_code == EXIT_OK
    assert "revision=2 を維持" in report.text
    assert "tunable_thresholds.min_total_score_to_publish: 58 → 60" in report.text
    # 差分を出しただけで、config は admin の編集値のまま
    assert repo.load().tunable_thresholds.min_total_score_to_publish == 58
    assert repo.load().meta.revision == 2


async def test_a_rerun_against_an_unchanged_config_reports_no_diff(
    repo: ConfigRepository, out: Output
) -> None:
    """同じ xlsx で dry を再実行しても「差分なし」で終わる（冪等）。"""
    await run(repo, apply=True, out=out)

    report = Output()
    assert await run(repo, out=report) == EXIT_OK
    assert "差分なし" in report.text


# --- 手順4-5 の失敗時は書かない ---------------------------------------------


async def test_a_validation_failure_writes_nothing(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """配点が §5.2 と違う xlsx は検証で落ち、`config.json` を作らない。"""

    def change_weight(workbook: Any) -> None:
        workbook["スコアリング軸"]["C5"] = 30  # 顧客関連度 25 → 30（合計105）

    exit_code = await run(repo, xlsx=edit_xlsx(change_weight), apply=True, out=out)

    assert exit_code == EXIT_VALIDATION_FAILED
    assert not repo.exists()
    assert "weight_sum_mismatch" in out.text
    assert "書き込んでいません" in out.text


async def test_a_wrong_declared_total_is_refused(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """xlsx の「合計」行が 100 でなければ、100 として通さない。"""

    def change_total(workbook: Any) -> None:
        workbook["スコアリング軸"]["C11"] = 90

    exit_code = await run(repo, xlsx=edit_xlsx(change_total), apply=True, out=out)

    assert exit_code == EXIT_VALIDATION_FAILED
    assert not repo.exists()
    assert "scoring_total" in out.text


async def test_a_changed_priority_is_caught_by_the_spec_check(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """初期優先度が §5.2 と違えば手順5（`validate_initial_config`）で落ちる。"""

    def change_priority(workbook: Any) -> None:
        workbook["情報カテゴリ"]["E5"] = "低"

    exit_code = await run(repo, xlsx=edit_xlsx(change_priority), apply=True, out=out)

    assert exit_code == EXIT_VALIDATION_FAILED
    assert not repo.exists()
    assert "initial_value_mismatch" in out.text
    assert "information_categories.0.priority" in out.text


async def test_a_changed_id_is_caught(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """カテゴリID を変えた xlsx は通らない（ID は中間xlsx 互換に直結）。"""

    def change_id(workbook: Any) -> None:
        workbook["情報カテゴリ"]["C5"] = "ai_major_company_models"

    exit_code = await run(repo, xlsx=edit_xlsx(change_id), apply=True, out=out)

    assert exit_code == EXIT_VALIDATION_FAILED
    assert not repo.exists()
    assert "information_categories.0.id" in out.text


# --- xlsx 側の不備は「読めなかった」として区別する ---------------------------


async def test_an_unknown_japanese_label_is_refused_instead_of_guessed(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """未知の除外区分は推測で ID を当てず、入力不備として止める。"""

    def unknown_severity(workbook: Any) -> None:
        workbook["除外ルール"]["B5"] = "たぶん除外"

    exit_code = await run(repo, xlsx=edit_xlsx(unknown_severity), apply=True, out=out)

    assert exit_code == EXIT_INVALID_INPUT
    assert not repo.exists()
    assert "たぶん除外" in out.text


async def test_a_missing_sheet_is_refused(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    def drop_sheet(workbook: Any) -> None:
        del workbook["必須タグ"]

    exit_code = await run(repo, xlsx=edit_xlsx(drop_sheet), apply=True, out=out)

    assert exit_code == EXIT_INVALID_INPUT
    assert not repo.exists()
    assert "必須タグ" in out.text


async def test_a_missing_column_is_refused(
    repo: ConfigRepository,
    out: Output,
    edit_xlsx: Callable[[Callable[[Any], None]], Path],
) -> None:
    """見出し名で列を引くので、列名が変わったら気づける。"""

    def rename_column(workbook: Any) -> None:
        workbook["スコアリング軸"]["C4"] = "点数"

    exit_code = await run(repo, xlsx=edit_xlsx(rename_column), apply=True, out=out)

    assert exit_code == EXIT_INVALID_INPUT
    assert not repo.exists()
    assert "配点" in out.text


async def test_a_missing_xlsx_is_refused(repo: ConfigRepository, out: Output) -> None:
    exit_code = await run(repo, xlsx=Path("no/such/file.xlsx"), apply=True, out=out)

    assert exit_code == EXIT_INVALID_INPUT
    assert not repo.exists()
    assert "docs/source/" in out.text


# --- 置き場 -------------------------------------------------------------------


def test_the_source_xlsx_is_committed_where_the_cli_expects_it() -> None:
    """既定の入力パスに実ファイルがある（2026-08-14 決定: `docs/source/`）。

    ここが無いと `make migrate-config` が初回から失敗する。
    """
    assert DEFAULT_XLSX.is_file()
    assert DEFAULT_XLSX.parent.name == "source"
