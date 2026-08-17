"""本番 cron のスケジュール（T-28。設計書 §8.1 ／ 仕様書 §13.1）。

crontab への登録手順はインフラ確定後（README の「TODO: 本番 cron 登録」）だが、
**スケジュールそのものは確定値**なので、README に書いた値が設計書とずれないよう
固定する。ずれると「毎週月曜 08:00 に当週を回す」という前提が静かに崩れ、
`period` の既定解決（当週 / 前月）と噛み合わなくなる。

⚠️ **ここで検査するのは表記の一致だけ。** 実際に cron が動くかは、インフラが
決まってからの運用側の確認事項。
"""

from datetime import date
from pathlib import Path

import pytest

from enterprise.entities.run_job import RunType, resolve_period

REPO_ROOT = Path(__file__).parents[2]
BACKEND_README = REPO_ROOT / "backend" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
DESIGN = REPO_ROOT / "docs" / "design.md"

# 設計書 §8.1「ジョブ定義」の cron 列（＝仕様書 §13.1 の YAML）。
WEEKLY_CRON = "0 8 * * MON"
MONTHLY_CRON = "0 9 1 * *"
TIMEZONE = "Asia/Tokyo"


@pytest.fixture(scope="module")
def design() -> str:
    return DESIGN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readmes() -> dict[str, str]:
    return {
        "backend/README.md": BACKEND_README.read_text(encoding="utf-8"),
        "README.md": ROOT_README.read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("expression", [WEEKLY_CRON, MONTHLY_CRON])
def test_the_documented_schedule_matches_the_design(
    design: str, expression: str
) -> None:
    """⚠️ **設計書 §8.1 の確定値。** README を直すときはこちらも確認すること。"""
    assert expression in design


@pytest.mark.parametrize("expression", [WEEKLY_CRON, MONTHLY_CRON, TIMEZONE])
def test_both_readmes_state_the_schedule(
    readmes: dict[str, str], expression: str
) -> None:
    """インフラ担当が README だけを見て登録できるように、両方へ書いておく。"""
    for name, text in readmes.items():
        assert expression in text, name


def test_the_cron_todo_is_visible(readmes: dict[str, str]) -> None:
    """T-28 の完了条件「README に『TODO: 本番 cron 登録』の項を追加」。"""
    for name, text in readmes.items():
        assert "TODO: 本番 cron 登録" in text, name


def test_the_schedule_and_the_default_period_agree() -> None:
    """cron の起動時刻で `period` の既定解決が意図どおりになること。

    ⚠️ **ここがずれると、月曜 08:00 の実行が先週ぶんを回す**（あるいは月初の
    実行が当月を回す）。cron 式（曜日・日）と `resolve_period()` の規則
    （当週 / 前月）は別々に決まっているので、噛み合いを1本の検査で押さえる。
    """
    # `0 8 * * MON` が起きるのは月曜。その日の「当週」は自分自身の週。
    monday = date(2026, 8, 17)
    assert monday.isoweekday() == 1
    assert resolve_period(RunType.WEEKLY, today=monday) == "2026-W34"

    # `0 9 1 * *` が起きるのは毎月1日。その日の「前月」は先月。
    first_of_month = date(2026, 8, 1)
    assert first_of_month.day == 1
    assert resolve_period(RunType.MONTHLY, today=first_of_month) == "2026-07"
