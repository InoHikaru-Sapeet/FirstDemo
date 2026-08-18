"""手編集した config.json を履歴へ記録する CLI（T-47。設計書 §4.3・§6.3）。

このコマンドが守るのは「ファイル（正）と改訂履歴（実行時の固定参照元）が
揃っていること」。重点は:

- **乖離あり**：手編集ぶんが新 revision として記録され、**ファイルの
  `meta.revision` も追随する**（記録後は `get_pinned()` が固定できる）
- **乖離なし**：何もしない（**冪等**。2回続けて `--apply` しても revision は1つ）
- **検証失敗**：ファイルも DB も触らない（設計判断A：補正して通さない）
- **dry が既定**：差分と新 revision 番号の予告だけで何も書かない
- **経路の再利用**：書き込みは `PUT /config` と同じ `UpdateConfigUsecase` →
  `ConfigRepository.save()`。履歴行・監査ログ `config_update` の形が揃う
- **旧スキーマのスナップショットとも差分が取れる**（＝実運用で起きた乖離そのもの。
  読めない行を基準にできないと、何が食い違っているのか報告できない）
"""

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapter.cli.record_config import (
    CLI_ACTOR,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_VALIDATION_FAILED,
    run,
)
from adapter.config_repository import ConfigRepository
from adapter.database.base import Base
from adapter.database.models.audit_log import AuditEventType, AuditLog
from adapter.database.models.config_revision import ConfigRevision
from adapter.storage.artifact_store import CONFIG_FILENAME, ArtifactStore
from application.usecases.update_config import UpdateConfigUsecase
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.json_document import DocumentParseError

# §5.2 の逐語コピー（T-04 / T-05 / T-11 / T-14 のテストと同じ実データ）。
SPEC_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture(scope="session")
def spec_raw() -> dict[str, Any]:
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


@pytest.fixture
def usecase(db: AsyncSession, repo: ConfigRepository) -> UpdateConfigUsecase:
    return UpdateConfigUsecase(db=db, repo=repo)


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


# --- 場面の組み立て -----------------------------------------------------------


@pytest.fixture
async def initial(
    repo: ConfigRepository, spec_raw: dict[str, Any]
) -> IntelligenceConfig:
    """revision=1 の config を作る（ファイルと履歴が揃った状態）。"""
    return await repo.create_initial(
        IntelligenceConfig.model_validate(copy.deepcopy(spec_raw))
    )


def hand_edit(repo: ConfigRepository, mutate: Any) -> dict[str, Any]:
    """`config.json` を**このコマンド以外の手段で**書き換える（＝手編集）。

    ⚠️ わざと `ConfigRepository` を通さない。通してしまうと履歴が付いてきて、
    このコマンドが直す対象（ファイルだけが進んだ状態）が再現できない。
    """
    data = json.loads(repo.path.read_text(encoding="utf-8"))
    mutate(data)
    repo.path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def read_file(repo: ConfigRepository) -> dict[str, Any]:
    return json.loads(repo.path.read_text(encoding="utf-8"))


async def revisions(db: AsyncSession) -> list[int]:
    rows = await db.execute(
        select(ConfigRevision.revision).order_by(ConfigRevision.revision)
    )
    return [row.revision for row in rows.all()]


async def audit_rows(db: AsyncSession) -> list[AuditLog]:
    rows = await db.execute(select(AuditLog).order_by(AuditLog.at))
    return list(rows.scalars().all())


# --- 乖離あり: 手編集ぶんが記録される -----------------------------------------


async def test_a_hand_edit_becomes_the_next_revision(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """手で直した内容が revision=2 として履歴に入り、ファイルもそれを指す。

    これが成り立つと `get_pinned()`（§6.3）が実行前に固定できる＝T-26 が
    `ConfigPinError` で止まらなくなる。
    """
    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"]["monthly"].__setitem__(
            "min_score_for_case", 65
        ),
    )

    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_OK
    assert await revisions(db) == [1, 2]
    # ファイルの meta.revision も 2 になっている（次の実行はこれを固定する）
    assert read_file(repo)["meta"]["revision"] == 2
    assert read_file(repo)["meta"]["updated_by"] == CLI_ACTOR
    # 履歴のスナップショットがファイルの中身と一致する（固定できる状態）
    pinned = await repo.get_pinned(2)
    assert pinned.tunable_thresholds.monthly.min_score_for_case == 65
    assert pinned.model_dump(mode="json") == read_file(repo)


async def test_the_history_row_summarizes_what_changed(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """`diff_summary` は**前 revision との差分**（ファイル同士の差ではない）。

    ⚠️ ここが空になる実装（差分の基準をファイルにする）だと、手編集の履歴が
    「何も変えていない revision」の列になり、履歴として役に立たない。
    """
    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"].__setitem__(
            "min_total_score_to_publish", 55
        ),
    )

    await run(repo, usecase, apply=True, out=out)

    row = await db.get(ConfigRevision, 2)
    assert row is not None
    assert row.diff_summary == "tunable_thresholds.min_total_score_to_publish 60→55"
    assert row.updated_by == CLI_ACTOR


async def test_the_audit_log_records_the_diff_against_the_previous_revision(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """監査ログ `config_update` の形が `PUT /config` と揃っている（§4.4）。

    `actor` だけが `cli:` 系で、`revision` / `diff` / `target` の形は同じ。
    """
    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"]["weekly"].__setitem__(
            "max_common_topics", 3
        ),
    )

    await run(repo, usecase, apply=True, out=out)

    logs = await audit_rows(db)
    assert len(logs) == 1
    assert logs[0].event_type == AuditEventType.CONFIG_UPDATE
    assert logs[0].actor == CLI_ACTOR
    assert logs[0].revision == 2
    assert logs[0].target == CONFIG_FILENAME
    assert logs[0].diff == {
        "tunable_thresholds.weekly.max_common_topics": {"before": 6, "after": 3}
    }


async def test_a_snapshot_written_under_an_older_schema_can_still_be_diffed(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """**実運用で起きた乖離そのもの**：履歴が旧形式（`target_industry`）。

    T-46 Step 3 で鍵が `target_industries` へ変わったため、revision=1 の
    スナップショットは現行スキーマで読めない（＝`get_pinned()` が落ちる＝
    パイプラインが止まる）。⚠️ **読めない行を基準に差分が取れないと、この
    コマンドは「何が食い違っているのか」を報告できないまま記録することになる。**
    """
    row = await db.get(ConfigRevision, 1)
    assert row is not None
    snapshot = copy.deepcopy(row.config_snapshot)
    snapshot["tunable_thresholds"].pop("target_industries")
    snapshot["tunable_thresholds"]["weekly"]["target_industry"] = "不動産"
    row.config_snapshot = snapshot
    await db.commit()

    # 現行スキーマでは固定できない（これが ConfigPinError の原因）
    with pytest.raises(DocumentParseError):
        await repo.get_pinned(1)

    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_OK
    # 鍵の入れ替えが差分として出ている（消えた鍵と増えた鍵の両方）
    assert "target_industry" in out.text
    assert "target_industries" in out.text
    # 記録後は現行スキーマで固定できる
    pinned = await repo.get_pinned(2)
    assert pinned.tunable_thresholds.industries == ("不動産",)


# --- 乖離なし・冪等 -----------------------------------------------------------


async def test_no_divergence_is_a_no_op(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """ファイルと履歴が揃っていれば、`--apply` でも何もしない。"""
    before = read_file(repo)

    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_OK
    assert await revisions(db) == [1]
    assert await audit_rows(db) == []
    assert read_file(repo) == before
    assert "記録済み・変更なし" in out.text


async def test_recording_twice_adds_only_one_revision(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """冪等：同じ手編集に対して2回 `--apply` しても revision は1つしか増えない。

    ⚠️ 増え続ける実装だと、cron や運用手順に入れた瞬間に履歴が汚染される。
    """
    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"]["dedup"].__setitem__(
            "lookback_weeks", 4
        ),
    )

    assert await run(repo, usecase, apply=True, out=out) == EXIT_OK
    assert await run(repo, usecase, apply=True, out=out) == EXIT_OK

    assert await revisions(db) == [1, 2]
    assert len(await audit_rows(db)) == 1


async def test_the_revision_pointer_alone_is_enough_to_record(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """中身が同じでも `meta.revision` が最新を指していなければ記録する。

    ⚠️ 中身だけを見る実装だと、この状態（ファイルが古い revision を指したまま）で
    「変更なし」と答えてしまい、実行は**別のスナップショットを固定して**走る。
    """
    # 履歴だけが 2 まで進み、ファイルは 1 を指したまま（中身は同じ）
    latest = await repo.save(
        IntelligenceConfig.model_validate(read_file(repo)),
        base_revision=1,
        updated_by="admin:usr_1",
    )
    assert latest.meta.revision == 2
    hand_edit(repo, lambda data: data["meta"].__setitem__("revision", 1))

    code = await run(repo, usecase, apply=False, out=out)

    assert code == EXIT_OK
    assert "meta.revision" in out.text
    assert "差分なし" in out.text


# --- dry が既定 ---------------------------------------------------------------


async def test_dry_is_the_default_and_writes_nothing(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """既定（`--apply` なし）は差分と新 revision 番号の予告だけ。"""
    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"].__setitem__(
            "min_total_score_to_publish", 58
        ),
    )
    before = read_file(repo)

    code = await run(repo, usecase, out=out)

    assert code == EXIT_OK
    assert await revisions(db) == [1]
    assert await audit_rows(db) == []
    assert read_file(repo) == before
    assert "tunable_thresholds.min_total_score_to_publish" in out.text
    assert "revision=2 になります" in out.text


# --- 検証失敗は書かない -------------------------------------------------------


async def test_a_cross_field_violation_stops_the_recording(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """Σweight≠100 の手編集は記録しない（T-05・設計判断A：補正もしない）。

    ⚠️ 通してしまうと、次の実行が**壊れた基準を固定参照して**走る。
    """

    def break_weights(data: dict[str, Any]) -> None:
        data["scoring_axes"][0]["weight"] = 5

    hand_edit(repo, break_weights)
    before = read_file(repo)

    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_VALIDATION_FAILED
    assert await revisions(db) == [1]
    assert await audit_rows(db) == []
    assert read_file(repo) == before
    assert "weight_sum_mismatch" in out.text
    # 値を補正して通していない
    assert read_file(repo)["scoring_axes"][0]["weight"] == 5


async def test_a_schema_violation_stops_the_recording(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """モデル検証（T-04）に落ちる手編集も記録しない。未知の鍵はここで落ちる。"""
    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"]["weekly"].__setitem__(
            "target_industry", "不動産"
        ),
    )

    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_VALIDATION_FAILED
    assert await revisions(db) == [1]
    assert await audit_rows(db) == []
    assert "構造検証エラー" in out.text


async def test_a_missing_config_is_reported_without_a_traceback(
    repo: ConfigRepository, usecase: UpdateConfigUsecase, out: Output
) -> None:
    """`config.json` が無い（初期投入前）ときは案内して終わる。"""
    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_INVALID_INPUT
    assert "中止しました" in out.text


# --- ファイルと履歴が食い違っていて、人が決めるべき場合 -----------------------


async def test_a_file_pointing_at_an_older_revision_is_refused(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """採番先の履歴行が既にあるなら拒否する（黙って上書きしない）。

    ⚠️ ファイルを古い revision へ手で戻した場合。**どちらを正にするかは人が
    決める**話なので、CLI は履歴を書き換えずに止まる。
    """
    await repo.save(
        IntelligenceConfig.model_validate(read_file(repo)),
        base_revision=1,
        updated_by="admin:usr_1",
    )
    # ファイルだけ revision=1 の中身を変えたものへ戻す（採番先は 2 ＝既にある）
    hand_edit(
        repo,
        lambda data: (
            data["meta"].__setitem__("revision", 1),
            data["tunable_thresholds"].__setitem__("min_total_score_to_publish", 51),
        ),
    )

    code = await run(repo, usecase, apply=True, out=out)

    assert code == EXIT_REFUSED
    assert await revisions(db) == [1, 2]
    assert len(await audit_rows(db)) == 0
    assert read_file(repo)["meta"]["revision"] == 1
    assert "記録できませんでした" in out.text


# --- 経路の再利用（二重実装していないこと）-----------------------------------


async def test_the_write_goes_through_the_update_config_usecase(
    repo: ConfigRepository,
    usecase: UpdateConfigUsecase,
    db: AsyncSession,
    initial: IntelligenceConfig,
    out: Output,
) -> None:
    """書き込みは `UpdateConfigUsecase` → `ConfigRepository` の1本を通る。

    ⚠️ 直接ファイルへ書く実装だと、**履歴も監査ログも残らないのに revision だけ
    進む**。ここではユースケースを呼ばない差し替えで「1件も残らない」ことを
    見て、経路を固定する。
    """

    class NeverCalled(UpdateConfigUsecase):
        async def record_current(  # type: ignore[override]
            self, actor: str, *, diff_base_data: dict[str, Any]
        ) -> IntelligenceConfig:
            raise AssertionError("ここを通らずに書き込んではいけない")

    hand_edit(
        repo,
        lambda data: data["tunable_thresholds"].__setitem__(
            "min_total_score_to_publish", 57
        ),
    )
    before = read_file(repo)

    with pytest.raises(AssertionError):
        await run(repo, NeverCalled(db=db, repo=repo), apply=True, out=out)

    assert read_file(repo) == before
    assert await revisions(db) == [1]
