"""ArtifactStore（成果物の置き場・原子的書き込み・世代退避・scratch 掃除）。"""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adapter.storage.artifact_store import (
    MONTHLY_HTML_NAME,
    WEEKLY_HTML_NAME,
    ArtifactNameFormat,
    ArtifactStore,
    ArtifactStoreError,
    validate_period,
)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(
        root=tmp_path / "artifacts",
        history_max_generations=3,
        scratch_ttl_hours=24,
        tz=UTC,
    )


# --- パス解決 -------------------------------------------------------------


def test_canonical_paths_use_the_names_the_spec_fixes(store: ArtifactStore) -> None:
    """PROMPT-3 が固定名を入力に読むため、正規名は仕様どおりであること。"""
    assert store.weekly_report_path().name == "weekly_ai_intelligence_report.xlsx"
    assert store.monthly_cases_path().name == "monthly_ai_leading_cases.xlsx"
    assert store.raw_articles_path("2026-W31").name == "raw_articles_2026-W31.json"
    assert store.validation_path("2026-W31").name == "validation_2026-W31.json"
    assert store.narrative_path("2026-W31").name == "narrative_2026-W31.json"
    assert (
        store.weekly_html_path("不動産", "2026-W31").name
        == "weekly_ai_intelligence_newsletter_不動産_2026-W31.html"
    )
    assert store.monthly_html_path("2026-07").name == "monthly_belief_2026-07.html"


def test_every_artifact_stays_under_the_root(store: ArtifactStore) -> None:
    paths = [
        store.weekly_report_path(),
        store.monthly_cases_path(),
        store.raw_articles_path("2026-W31"),
        store.validation_path("2026-07"),
        store.narrative_path("2026-07"),
        store.weekly_html_path("不動産", "2026-W31"),
        store.monthly_html_path("2026-07"),
        store.dry_run_dir("dry_abc"),
    ]
    for path in paths:
        assert store.root in path.parents


@pytest.mark.parametrize("period", ["2026-W31", "2026-W01", "2026-07", "2026-12"])
def test_valid_periods(period: str) -> None:
    assert validate_period(period) == period


@pytest.mark.parametrize(
    "period",
    ["", "2026", "26-W31", "2026-W3", "2026-7", "2026-W31 ", "../etc", "2026/07"],
)
def test_invalid_periods_are_rejected(period: str) -> None:
    with pytest.raises(ArtifactStoreError):
        validate_period(period)


@pytest.mark.parametrize("industry", ["../secrets", "a/b", "a\\b", "", "."])
def test_path_traversal_via_industry_is_rejected(
    store: ArtifactStore, industry: str
) -> None:
    with pytest.raises(ArtifactStoreError):
        store.weekly_html_path(industry, "2026-W31")


def test_path_traversal_via_dry_run_id_is_rejected(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactStoreError):
        store.dry_run_dir("../../escape")


# --- 正規名の書式（生成・照合・探索が1つの定義から導かれること）--------------
#
# ⚠️ T-27 で「`WEEKLY_HTML_RE` と `weekly_html_path()` が同じ形を2箇所に持って
# いる」と報告された件の固定。**書式を変えるとこの節の複数のテストが同時に
# 落ちる**（＝片方だけ変えることが構造的にできない）ことを確かめる。


@pytest.mark.parametrize(
    ("industry", "period"),
    [("不動産", "2026-W31"), ("製造", "2026-W01"), ("a b", "2025-W52")],
)
def test_generated_weekly_html_names_are_recognised_by_the_same_format(
    store: ArtifactStore, industry: str, period: str
) -> None:
    """出した名前を自分で認識できること（配信の許可リストが 404 を出さない）。"""
    name = store.weekly_html_path(industry, period).name

    assert store.is_servable(name)
    assert WEEKLY_HTML_NAME.parse(name) == {"industry": industry, "period": period}


def test_generated_monthly_html_names_are_recognised_by_the_same_format(
    store: ArtifactStore,
) -> None:
    name = store.monthly_html_path("2026-07").name

    assert store.is_servable(name)
    assert MONTHLY_HTML_NAME.parse(name) == {"period": "2026-07"}


def test_the_weekly_glob_finds_exactly_the_names_the_format_generates(
    store: ArtifactStore,
) -> None:
    """探索（glob）も同じ書式から導かれること。

    ⚠️ ここが生成とずれると、`GET /reports/{period}` の一覧から HTML が消える
    （ファイルは在るのに見つからない）。
    """
    for industry in ("不動産", "製造"):
        store.write_text(store.weekly_html_path(industry, "2026-W31"), "<html></html>")
    # 別の週・別の種類は拾わないこと
    store.write_text(store.weekly_html_path("不動産", "2026-W30"), "<html></html>")
    store.write_text(store.monthly_html_path("2026-07"), "<html></html>")

    found = store.weekly_html_paths("2026-W31")

    assert [path.name for path in found] == [
        store.weekly_html_path("不動産", "2026-W31").name,
        store.weekly_html_path("製造", "2026-W31").name,
    ]


# --- 一覧の材料（T-36）--------------------------------------------------------


def test_rendered_periods_are_empty_before_anything_is_written(
    store: ArtifactStore,
) -> None:
    assert store.rendered_periods() == []


def test_rendered_periods_come_from_the_html_that_is_actually_there(
    store: ArtifactStore,
) -> None:
    """⚠️ **新しい号が先**。週刊は業界ごとに何通あっても period は1つ。"""
    store.write_text(store.weekly_html_path("不動産", "2026-W30"), "<html></html>")
    store.write_text(store.weekly_html_path("不動産", "2026-W31"), "<html></html>")
    store.write_text(store.weekly_html_path("製造", "2026-W31"), "<html></html>")
    store.write_text(store.monthly_html_path("2026-07"), "<html></html>")

    assert store.rendered_periods() == ["2026-W31", "2026-W30", "2026-07"]


def test_rendered_periods_ignore_artifacts_that_are_not_generated_html(
    store: ArtifactStore,
) -> None:
    """⚠️ 中間xlsx・生の成果物・履歴は一覧の材料にしない。"""
    store.write_text(store.config_path(), "{}")
    store.write_text(store.raw_articles_path("2026-W31"), "{}")
    store.write_text(store.narrative_path("2026-W31"), "{}")
    store.write_text(store.validation_path("2026-W31"), "{}")
    store.write_bytes(store.weekly_report_path(), b"xlsx")

    assert store.rendered_periods() == []


@pytest.mark.parametrize(
    "filename",
    [
        # 週次の period 表記でない（月次の表記・桁不足・接頭辞違い）
        "weekly_ai_intelligence_newsletter_不動産_2026-07.html",
        "weekly_ai_intelligence_newsletter_不動産_2026-W3.html",
        "weekly_ai_intelligence_newsletter_2026-W31.html",  # industry が無い
        "monthly_belief_2026-W31.html",
        "monthly_belief_2026-7.html",
        "weekly_ai_intelligence_newsletter_不動産_2026-W31.htm",
    ],
)
def test_names_outside_the_format_are_not_servable(
    store: ArtifactStore, filename: str
) -> None:
    assert not store.is_servable(filename)


def test_the_period_pattern_comes_from_the_period_entity() -> None:
    """ファイル名のパターンに period の表記を書き写していないこと。

    `enterprise.entities.period` が表記の定義を持つ（モジュール冒頭の⚠️）。
    ここに写しがあると、表記を変えたときに片方だけ古いまま残る。
    """
    assert WEEKLY_HTML_NAME.parse("weekly_ai_intelligence_newsletter_x_2026-W31.html")
    assert (
        WEEKLY_HTML_NAME.parse("weekly_ai_intelligence_newsletter_x_2026-31.html")
        is None
    )


def test_a_format_rejects_fields_without_a_pattern() -> None:
    """書式とパターンの対応漏れを import 時に落とす。"""
    with pytest.raises(ArtifactStoreError):
        ArtifactNameFormat("{a}_{b}.html", a=r".+")
    with pytest.raises(ArtifactStoreError):
        ArtifactNameFormat("{a}.html", a=r".+", b=r".+")
    with pytest.raises(ArtifactStoreError):
        ArtifactNameFormat("{a}_{a}.html", a=r".+")


def test_a_format_rejects_incomplete_values() -> None:
    """フィールドを渡し忘れた生成を通さない（`_2026-W31.html` を作らせない）。"""
    with pytest.raises(ArtifactStoreError):
        WEEKLY_HTML_NAME.format(period="2026-W31")
    with pytest.raises(ArtifactStoreError):
        WEEKLY_HTML_NAME.glob(industry="不動産", nope="x")


# --- 原子的書き込み -------------------------------------------------------


def test_write_text_round_trips_as_utf8(store: ArtifactStore) -> None:
    path = store.monthly_html_path("2026-07")
    store.write_text(path, "月刊ビリーフ by Sapeet")
    assert store.read_text(path) == "月刊ビリーフ by Sapeet"
    assert path.read_bytes().decode("utf-8") == "月刊ビリーフ by Sapeet"


def test_write_creates_missing_directories(store: ArtifactStore) -> None:
    assert not store.root.exists()
    store.write_text(store.raw_articles_path("2026-W31"), "[]")
    assert store.raw_articles_path("2026-W31").is_file()


def test_failed_write_leaves_the_previous_file_intact(store: ArtifactStore) -> None:
    """途中で失敗した成果物が完成品として読まれないこと。"""
    path = store.raw_articles_path("2026-W31")
    store.write_text(path, "元の内容")

    with pytest.raises(RuntimeError):
        with store.atomic_write(path) as tmp_path:
            tmp_path.write_text("壊れかけの内容", encoding="utf-8")
            raise RuntimeError("生成に失敗")

    assert store.read_text(path) == "元の内容"


def test_failed_write_leaves_no_temp_files(store: ArtifactStore) -> None:
    path = store.raw_articles_path("2026-W31")
    with pytest.raises(RuntimeError):
        with store.atomic_write(path):
            raise RuntimeError("生成に失敗")
    assert list(store.root.iterdir()) == []


def test_write_overwrites_in_place(store: ArtifactStore) -> None:
    path = store.weekly_report_path()
    store.write_bytes(path, b"v1")
    store.write_bytes(path, b"v2")
    assert store.read_bytes(path) == b"v2"
    assert [p.name for p in store.root.iterdir()] == [path.name]


# --- 世代退避（設計判断B）------------------------------------------------


def test_archive_is_a_noop_on_first_run(store: ArtifactStore) -> None:
    path = store.weekly_report_path()
    assert store.archive(path, period="2026-W31", revision=1, run_id="job_1") is None


def test_archive_snapshots_the_previous_content(store: ArtifactStore) -> None:
    path = store.weekly_report_path()
    store.write_bytes(path, b"run-1")

    archived = store.archive(path, period="2026-W31", revision=2, run_id="job_1")
    store.write_bytes(path, b"run-2")

    assert archived is not None
    assert archived.read_bytes() == b"run-1"
    assert archived.parent.name == "2_job_1"
    assert archived.parent.parent.name == "2026-W31"
    # 正規名は上書きされている（PROMPT-3 の入力が固定名のため）
    assert store.read_bytes(path) == b"run-2"


def test_history_is_capped_at_the_configured_generations(
    store: ArtifactStore,
) -> None:
    path = store.weekly_report_path()
    for run in range(1, 6):
        store.write_bytes(path, f"結果{run}".encode())
        store.archive(path, period="2026-W31", revision=run, run_id=f"job_{run}")
        # 世代の新旧を mtime で判定するため、順序が潰れないようずらす
        time.sleep(0.01)

    generations = sorted(p.name for p in (store.history_root / "2026-W31").iterdir())
    assert len(generations) == 3
    assert generations == ["3_job_3", "4_job_4", "5_job_5"]


def test_history_is_kept_per_period(store: ArtifactStore) -> None:
    path = store.weekly_report_path()
    store.write_bytes(path, b"W31")
    store.archive(path, period="2026-W31", revision=1, run_id="job_1")
    store.write_bytes(path, b"W32")
    store.archive(path, period="2026-W32", revision=1, run_id="job_2")

    assert (store.history_root / "2026-W31" / "1_job_1").is_dir()
    assert (store.history_root / "2026-W32" / "1_job_2").is_dir()


def test_archive_rejects_unsafe_run_id(store: ArtifactStore) -> None:
    path = store.weekly_report_path()
    store.write_bytes(path, b"x")
    with pytest.raises(ArtifactStoreError):
        store.archive(path, period="2026-W31", revision=1, run_id="../escape")


# --- scratch の TTL 掃除（設計判断C）-------------------------------------


def _age_directory(path: Path, *, hours: float) -> None:
    past = (datetime.now(tz=UTC) - timedelta(hours=hours)).timestamp()
    os.utime(path, (past, past))


def test_purge_removes_only_expired_dry_runs(store: ArtifactStore) -> None:
    fresh = store.dry_run_dir("dry_fresh")
    expired = store.dry_run_dir("dry_expired")
    for directory in (fresh, expired):
        directory.mkdir(parents=True)
        (directory / "result.xlsx").write_bytes(b"x")
    _age_directory(expired, hours=25)

    removed = store.purge_expired_scratch()

    assert removed == [expired]
    assert not expired.exists()
    assert fresh.exists()


def test_purge_is_safe_when_nothing_was_written(store: ArtifactStore) -> None:
    assert store.purge_expired_scratch() == []


def test_dry_run_output_never_touches_canonical_artifacts(
    store: ArtifactStore,
) -> None:
    canonical = store.weekly_report_path()
    store.write_bytes(canonical, b"canonical")

    dry_run = store.dry_run_dir("dry_1")
    dry_run.mkdir(parents=True)
    (dry_run / "result.xlsx").write_bytes(b"dry-run")

    assert store.read_bytes(canonical) == b"canonical"
    assert store.scratch_root in dry_run.parents
