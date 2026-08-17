"""プロンプトの切り出しと版管理（T-30 ／ 設計書 §9.1・§9.2）。

このテストが守っているのは **「PM が読む `prompts/*.md`」と「実際に走るプロンプト」を
乖離させない**という T-30 の必須条件。`prompts/*.md` は生成物で、正はコード
（`application.usecases.*` の `build_*_prompt()`）なので、次の3つを固定する:

- **コミット済みの `prompts/*.md` が、いまのコードの描画結果と完全一致する**
  （コードだけ直して `make prompts` を忘れたら落ちる）
- **実行時に使われているプロンプトが `prompts/` に全部載っている**
  （新しい AI 呼び出しを足して載せ忘れたら落ちる）
- **`prompt_version` はコードの定数から取っている**（Markdown 側に写しを持たない）

⚠️ **実際の `claude` は起動しない。** プロンプトの**組み立て**だけを見るテストで、
AI 呼び出しは1回も行わない。
"""

import re
from pathlib import Path

import pytest

from adapter.cli.export_prompts import (
    COMMON_STEM,
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT_DIR,
    EXIT_OK,
    EXIT_STALE,
    PROMPT_DOCS,
    load_config,
    main,
    render_all,
    render_document,
    stale_files,
    write_prompts,
)
from application.usecases.classify_and_score import (
    PROMPT_VERSION as CLASSIFY_PROMPT_VERSION,
)
from application.usecases.crawl import PROMPT_VERSION as CRAWL_PROMPT_VERSION
from application.usecases.monthly_cases import (
    CASE_PROMPT_VERSION,
    CHAPTER_PROMPT_VERSION,
)
from application.usecases.narrative import (
    MONTHLY_NARRATIVE_PROMPT_VERSION,
    WEEKLY_NARRATIVE_PROMPT_VERSION,
)
from enterprise.entities.config import IntelligenceConfig

ENCODING = "utf-8"

# 実行経路にある AI 呼び出しの `prompt_version` 定数。**ここに載っているものは
# すべて `prompts/` に出ていること**を下で突き合わせる（載せ忘れの検出）。
LIVE_PROMPT_VERSIONS = {
    "PROMPT-1": CRAWL_PROMPT_VERSION,
    "PROMPT-2": CLASSIFY_PROMPT_VERSION,
    "PROMPT-2-NARRATIVE-WEEKLY": WEEKLY_NARRATIVE_PROMPT_VERSION,
    "PROMPT-2-NARRATIVE-MONTHLY": MONTHLY_NARRATIVE_PROMPT_VERSION,
    "PROMPT-2-MONTHLY-CASE": CASE_PROMPT_VERSION,
    "PROMPT-2-MONTHLY-CHAPTERS": CHAPTER_PROMPT_VERSION,
}

# 手書きで維持するファイル（生成の対象外）。PROMPT-3 は実行経路に無く、描画元に
# なるコードが存在しない（render は決定的 Python テンプレート＝T-24/T-25）。
HAND_WRITTEN = {"README.md", "PROMPT-3-WEEKLY.md", "PROMPT-3-MONTHLY.md"}

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@pytest.fixture(scope="session")
def config() -> IntelligenceConfig:
    """描画に使う config（仕様書 §5.2 の確定値）。"""
    return load_config(DEFAULT_CONFIG)


# --- 乖離の検出（このテストの主目的）-----------------------------------------


def test_committed_prompts_match_the_code(config: IntelligenceConfig) -> None:
    """コミット済みの `prompts/*.md` が、いまのコードの描画結果と一致する。

    ⚠️ **落ちたら `cd backend && make prompts` で生成し直し、`prompt_version` を
    上げてコミットすること。** ここが緩むと、PM が読むファイルと実際に送られる
    プロンプトが静かにずれる（T-30 の必須条件）。
    """
    stale = stale_files(config, DEFAULT_OUTPUT_DIR)
    assert stale == [], (
        "prompts/ がコードと食い違っています（`make prompts` で生成し直してください）: "
        f"{[path.name for path in stale]}"
    )


def test_a_prompt_change_makes_the_committed_file_stale(
    config: IntelligenceConfig, tmp_path: Path
) -> None:
    """本文が1文字変わればコミット済みファイルは stale になる。

    上のテストが**実効的である**ことの確認（描画結果を比べていない実装だと、
    プロンプトを変えても通ってしまう）。
    """
    write_prompts(config, tmp_path)
    assert stale_files(config, tmp_path) == []

    target = tmp_path / "PROMPT-1.md"
    target.write_text(
        target.read_text(encoding=ENCODING).replace("リサーチャー", "調査員"),
        encoding=ENCODING,
    )
    assert stale_files(config, tmp_path) == [target]


def test_every_live_prompt_is_written_out(config: IntelligenceConfig) -> None:
    """実行経路にあるプロンプトが `prompts/` に全部載っている。

    ⚠️ **AI 呼び出しを足したら `PROMPT_DOCS` にも足すこと。** 載せ忘れたものは
    PM の目に触れないまま走り続ける（それが T-30 で塞ぎたい乖離そのもの）。
    """
    written = {doc.stem for doc in PROMPT_DOCS}
    assert set(LIVE_PROMPT_VERSIONS) <= written


def test_versions_come_from_the_code(config: IntelligenceConfig) -> None:
    """`prompt_version` はコードの定数がそのまま出ている（Markdown 側に写しが無い）。"""
    by_stem = {doc.stem: doc for doc in PROMPT_DOCS}
    for stem, version in LIVE_PROMPT_VERSIONS.items():
        assert by_stem[stem].version == version
        assert SEMVER.match(version), f"{stem} の版が semver ではありません: {version}"

    documents = render_all(config)
    for stem, version in LIVE_PROMPT_VERSIONS.items():
        assert f"| `prompt_version` | `{version}` |" in documents[f"{stem}.md"]


def test_hand_written_files_are_not_generated() -> None:
    """PROMPT-3 と README は生成の対象外（手書きのまま残る）。"""
    generated = {f"{doc.stem}.md" for doc in PROMPT_DOCS}
    assert generated.isdisjoint(HAND_WRITTEN)
    for name in HAND_WRITTEN:
        assert (DEFAULT_OUTPUT_DIR / name).is_file(), f"{name} がありません"


def test_prompt_3_is_marked_unused() -> None:
    """PROMPT-3 系に「未使用・render は決定的 Python」の注記がある（T-30 完了条件）。"""
    for name in ("PROMPT-3-WEEKLY.md", "PROMPT-3-MONTHLY.md"):
        text = (DEFAULT_OUTPUT_DIR / name).read_text(encoding=ENCODING)
        assert "未使用" in text
        assert "実行経路にありません" in text
        assert "決定的 Python" in text


# --- 生成物の中身 -------------------------------------------------------------


def test_documents_carry_the_required_header(config: IntelligenceConfig) -> None:
    """各ファイルに版・用途・変数の注入元・最終更新日が載っている（設計書 §9.1）。"""
    documents = render_all(config)
    for doc in PROMPT_DOCS:
        text = documents[f"{doc.stem}.md"]
        assert f"| `prompt_version` | `{doc.version}` |" in text
        assert f"| 用途（ステージ） | {doc.stage} |" in text
        assert f"| 最終更新日 | {doc.updated} |" in text
        assert "## 変数と注入元（設計書 §9.1）" in text
        for name, source in doc.variables:
            assert f"| {name} | {source} |" in text


def test_bodies_are_the_real_prompts(config: IntelligenceConfig) -> None:
    """本文は組み立て関数の戻り値そのもの（要約・言い換えをしていない）。"""
    documents = render_all(config)
    for doc in PROMPT_DOCS:
        text = documents[f"{doc.stem}.md"]
        for _, render in doc.bodies:
            assert render(config) in text


def test_config_values_are_rendered(config: IntelligenceConfig) -> None:
    """config 由来の行が実値で出ている（PM が確定値を読める）。

    プロンプトは「実行時点の config の値をそのまま使う」形で組み立てられている
    （仕様書 §13.3）ので、描画にも実値が出ていないと本文を読んだことにならない。
    """
    classification = render_all(config)["PROMPT-2.md"]
    for category in config.information_categories:
        assert category.id in classification
    for axis in config.scoring_axes:
        assert axis.label in classification
    for rule in config.exclusion_rules:
        assert rule.name in classification


def test_severity_is_not_shown_to_the_model(config: IntelligenceConfig) -> None:
    """除外ルールの `severity` / `enabled` が本文に出ていない（決定1 の境界）。

    ⚠️ ここが崩れると「これは full_exclude だから当たったことにしないでおこう」と
    いう逆算の余地が生まれる。`prompts/` は PM が読むものなので、**本文にその値が
    無いこと**もここで固定しておく。
    """
    body = next(
        render(config)
        for doc in PROMPT_DOCS
        if doc.stem == "PROMPT-2"
        for _, render in doc.bodies
    )
    assert "full_exclude" not in body
    assert "default_exclude" not in body
    assert "enabled" not in body


def test_output_instructions_are_documented_once(config: IntelligenceConfig) -> None:
    """出力形式の指示は共通ファイルにだけあり、各本文には入っていない。

    ⚠️ 各プロンプトへ書き足すと二重指示になり、AI クライアントの実装を差し替えた
    ときに片方だけ残る（各 usecase モジュールの ⚠️ と同じ約束）。
    """
    documents = render_all(config)
    assert "JSON Schema:" in documents[f"{COMMON_STEM}.md"]
    for doc in PROMPT_DOCS:
        if doc.stem == COMMON_STEM:
            continue
        for _, render in doc.bodies:
            assert "JSON Schema" not in render(config)


def test_bodies_are_fenced_so_markdown_does_not_eat_them(
    config: IntelligenceConfig,
) -> None:
    """本文はコードフェンスの中にある（`**強調**` が Markdown に食われない）。

    ⚠️ 出力形式の指示は本文に ``` を含むので、囲みは4連バッククォートでないと
    途中で閉じてしまう。
    """
    for text in render_all(config).values():
        assert "````text" in text
    assert "```" in render_document(
        next(doc for doc in PROMPT_DOCS if doc.stem == COMMON_STEM), config
    )


# --- CLI ----------------------------------------------------------------------


def test_check_passes_for_the_committed_files() -> None:
    """`make prompts-check` 相当が通る。"""
    assert main(["--check"]) == EXIT_OK


def test_check_reports_stale_files(tmp_path: Path) -> None:
    """描画結果と違うファイル（無いファイルを含む）は stale として 1 を返す。"""
    assert main(["--check", "--output-dir", str(tmp_path)]) == EXIT_STALE

    assert main(["--output-dir", str(tmp_path)]) == EXIT_OK
    assert main(["--check", "--output-dir", str(tmp_path)]) == EXIT_OK


def test_write_creates_every_document(
    config: IntelligenceConfig, tmp_path: Path
) -> None:
    """書き出しは `PROMPT_DOCS` のぶんだけ作る。"""
    written = write_prompts(config, tmp_path)
    assert {path.name for path in written} == {f"{doc.stem}.md" for doc in PROMPT_DOCS}
    assert all(path.read_text(encoding=ENCODING) for path in written)
