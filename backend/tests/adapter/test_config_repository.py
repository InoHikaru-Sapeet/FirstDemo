"""ConfigRepository（config.json の読み書き・楽観ロック・改訂履歴・ピン留め。T-11）。

重点は「破ると config の一貫性・再現性の建前が壊れる」性質:

- **ファイルが正**（現行 config は DB を見ずに読める）
- **検証を通らない config を書かない**、かつ**値を補正しない**（設計判断A）
- **`meta` はサーバが打つ**（呼び出し元が revision を偽装できない）
- **楽観ロック**：`base_revision` 不一致は書かずに競合として通知（設計書 §6.3）
- **ピン留め**：固定した revision は、その後 config が変わっても内容が動かない（§14）
- **履歴一覧に config の中身を載せない**（§3.3 の items は4項目）

基準となる入力は仕様書 §5.2 の確定 config
（`tests/enterprise/data/config_initial.json`）。
T-04 / T-05 のテストと同じ実データを使う。
"""

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapter.config_repository import (
    ConfigAlreadyExistsError,
    ConfigNotFoundError,
    ConfigRepository,
    ConfigRevisionAlreadyRecordedError,
    ConfigRevisionConflictError,
    ConfigRevisionNotFoundError,
    RevisionSummary,
    diff_configs,
    flatten_config,
    summarize_diff,
)
from adapter.database.base import Base
from adapter.database.models.config_revision import ConfigRevision
from adapter.storage.artifact_store import ArtifactStore
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.config_validation import ConfigValidationError
from enterprise.entities.json_document import DocumentParseError

# T-04 / T-05 と同じ実データ（仕様書 §5.2 の確定 config）を基準にする。
INITIAL_CONFIG_PATH = (
    Path(__file__).parents[1] / "enterprise" / "data" / "config_initial.json"
)

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def raw(initial_raw: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(initial_raw)


@pytest.fixture
def config(raw: dict[str, Any]) -> IntelligenceConfig:
    return IntelligenceConfig.model_validate(raw)


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


async def revision_rows(db: AsyncSession) -> list[ConfigRevision]:
    return list(
        (await db.execute(select(ConfigRevision).order_by(ConfigRevision.revision)))
        .scalars()
        .all()
    )


def with_weight_sum(config: IntelligenceConfig, *, delta: int) -> IntelligenceConfig:
    """先頭軸の weight をずらして Σweight ≠ 100 の config を作る。

    モデル検証（T-04）は通り、クロスフィールド検証（T-05）だけが落ちる状態。
    """
    axes = [axis.model_copy() for axis in config.scoring_axes]
    axes[0] = axes[0].model_copy(update={"weight": axes[0].weight + delta})
    return config.model_copy(update={"scoring_axes": axes})


# --- 置き場（ファイルが正 ／ ArtifactStore 経由）---------------------------


def test_config_lives_in_the_artifact_root_as_a_file(
    repo: ConfigRepository, store: ArtifactStore
) -> None:
    """config.json は DB ではなく成果物ルート直下のファイル（§1.1「永続化」）。"""
    assert repo.path == store.root / "config.json"
    assert repo.path.name == "config.json"


async def test_the_current_config_is_read_from_the_file_not_the_database(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """**ファイルが正。** 履歴行を消しても現行 config は読める。"""
    await repo.create_initial(config)

    for row in await revision_rows(db):
        await db.delete(row)
    await db.commit()

    loaded = repo.load()
    assert loaded.meta.revision == 1
    assert await revision_rows(db) == []


# --- 読み込み -------------------------------------------------------------


def test_loading_a_missing_config_says_so(repo: ConfigRepository) -> None:
    with pytest.raises(ConfigNotFoundError):
        repo.load()


def test_a_broken_json_file_fails_with_a_path(repo: ConfigRepository) -> None:
    """壊れたファイルを黙って通さない（T-06 の共通処理を経由する）。"""
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    repo.path.write_text('{"schema_version": ', encoding="utf-8")

    with pytest.raises(DocumentParseError) as excinfo:
        repo.load()
    assert excinfo.value.issues


def test_an_unknown_key_is_rejected_on_read(
    repo: ConfigRepository, raw: dict[str, Any]
) -> None:
    """タイポした設定キーを黙って無視しない（§2.1 additionalProperties: false）。"""
    raw["tunable_thresholds"]["min_total_score_to_pubish"] = 60  # typo
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    repo.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DocumentParseError):
        repo.load()


def test_a_hand_broken_cross_field_rule_can_still_be_read(
    repo: ConfigRepository, config: IntelligenceConfig
) -> None:
    """Σweight が崩れた config も**読める**。

    読めなくすると `GET /config` が 500 になり、admin が管理画面から直せなくなる。
    保存側（`save`）が必ず検証するので、この経路から壊れた config は入らない。
    """
    broken = with_weight_sum(config, delta=+1)
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    repo.path.write_text(
        json.dumps(broken.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )

    assert sum(axis.weight for axis in repo.load().scoring_axes) == 101


# --- 初期投入 -------------------------------------------------------------


async def test_create_initial_writes_revision_1_with_no_author(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """設計書 §10.3 手順6: revision=1 / updated_by=null / updated_at=移行時刻。"""
    saved = await repo.create_initial(config)

    assert saved.meta.revision == 1
    assert saved.meta.updated_by is None
    assert saved.meta.updated_at is not None
    assert saved.meta.updated_at.tzinfo is not None

    assert repo.load().meta.revision == 1
    rows = await revision_rows(db)
    assert [row.revision for row in rows] == [1]
    assert rows[0].updated_by is None
    assert rows[0].diff_summary is None


async def test_create_initial_refuses_to_overwrite_an_existing_config(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """既存の判断基準を黙って上書きしない（§10.4 冪等性）。"""
    await repo.create_initial(config)
    before = repo.path.read_text(encoding="utf-8")

    with pytest.raises(ConfigAlreadyExistsError):
        await repo.create_initial(config, updated_by="admin:usr_a")

    assert repo.path.read_text(encoding="utf-8") == before
    assert [row.revision for row in await revision_rows(db)] == [1]


async def test_create_initial_validates_before_writing_anything(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """検証失敗時は書き込まず中断（設計書 §10.4）。ファイルも履歴も作らない。"""
    with pytest.raises(ConfigValidationError):
        await repo.create_initial(with_weight_sum(config, delta=+5))

    assert not repo.exists()
    assert await revision_rows(db) == []


async def test_the_written_file_is_utf8_json_in_the_app_timezone(
    repo: ConfigRepository, config: IntelligenceConfig
) -> None:
    """入出力は UTF-8、日時は Asia/Tokyo（設計書 §14）。日本語をエスケープしない。"""
    await repo.create_initial(config)

    text = repo.path.read_text(encoding="utf-8")
    assert "不動産" in text
    assert "\\u" not in text
    assert "+09:00" in text


async def test_the_database_keeps_the_same_instant_in_utc(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """ファイルは +09:00、DB は UTC（`UtcDateTime`）。同じ瞬間を指すこと。"""
    saved = await repo.create_initial(config)

    row = (await revision_rows(db))[0]
    assert row.updated_at.tzinfo is not None
    assert saved.meta.updated_at is not None
    assert row.updated_at == saved.meta.updated_at
    assert row.updated_at.utcoffset() == UTC.utcoffset(None)


# --- 保存（revision 採番・楽観ロック）------------------------------------


async def test_saving_bumps_the_revision_and_records_the_author(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    await repo.create_initial(config)
    current = repo.load()

    updated = current.model_copy(
        update={
            "tunable_thresholds": current.tunable_thresholds.model_copy(
                update={"min_total_score_to_publish": 55}
            )
        }
    )
    saved = await repo.save(updated, base_revision=1, updated_by="admin:usr_a")

    assert saved.meta.revision == 2
    assert saved.meta.updated_by == "admin:usr_a"
    assert saved.tunable_thresholds.min_total_score_to_publish == 55

    reloaded = repo.load()
    assert reloaded.meta.revision == 2
    assert reloaded.tunable_thresholds.min_total_score_to_publish == 55

    rows = await revision_rows(db)
    assert [row.revision for row in rows] == [1, 2]
    assert rows[1].updated_by == "admin:usr_a"
    assert rows[1].diff_summary is not None
    assert "min_total_score_to_publish 60→55" in rows[1].diff_summary


async def test_consecutive_saves_number_revisions_in_order(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    await repo.create_initial(config)

    for expected, score in ((2, 59), (3, 58), (4, 57)):
        current = repo.load()
        candidate = current.model_copy(
            update={
                "tunable_thresholds": current.tunable_thresholds.model_copy(
                    update={"min_total_score_to_publish": score}
                )
            }
        )
        saved = await repo.save(
            candidate, base_revision=current.meta.revision, updated_by="admin:usr_a"
        )
        assert saved.meta.revision == expected

    assert [row.revision for row in await revision_rows(db)] == [1, 2, 3, 4]


async def test_a_stale_base_revision_is_a_conflict_and_writes_nothing(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """楽観ロック（設計書 §4.3・§6.3）。**後勝ちにしない。**"""
    await repo.create_initial(config)
    first = repo.load()
    await repo.save(first, base_revision=1, updated_by="admin:usr_a")
    before = repo.path.read_text(encoding="utf-8")

    with pytest.raises(ConfigRevisionConflictError) as excinfo:
        await repo.save(first, base_revision=1, updated_by="admin:usr_b")

    # 409 のボディに載せる現行 revision（設計書 §3.3）。
    assert excinfo.value.base_revision == 1
    assert excinfo.value.current_revision == 2
    assert repo.path.read_text(encoding="utf-8") == before
    assert [row.revision for row in await revision_rows(db)] == [1, 2]


async def test_saving_rejects_a_cross_field_violation_without_normalizing(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """設計判断A: 合計≠100 は保存拒否。**按分して補正しない。**"""
    await repo.create_initial(config)
    before = repo.path.read_text(encoding="utf-8")
    candidate = with_weight_sum(repo.load(), delta=+10)

    with pytest.raises(ConfigValidationError) as excinfo:
        await repo.save(candidate, base_revision=1, updated_by="admin:usr_a")

    assert [issue.path for issue in excinfo.value.issues] == ["scoring_axes"]
    # 入力にも触らない（正規化された別物を書かない）。
    assert sum(axis.weight for axis in candidate.scoring_axes) == 110
    assert repo.path.read_text(encoding="utf-8") == before
    assert [row.revision for row in await revision_rows(db)] == [1]


async def test_the_caller_cannot_dictate_the_revision_or_the_author(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """`meta` はサーバが打つ。偽装した revision / updated_by は採用しない。"""
    await repo.create_initial(config)
    current = repo.load()
    spoofed = current.model_copy(
        update={
            "meta": current.meta.model_copy(
                update={
                    "revision": 999,
                    "updated_by": "attacker",
                    "updated_at": datetime(2000, 1, 1, tzinfo=JST),
                }
            )
        }
    )

    saved = await repo.save(spoofed, base_revision=1, updated_by="admin:usr_a")

    assert saved.meta.revision == 2
    assert saved.meta.updated_by == "admin:usr_a"
    assert saved.meta.updated_at is not None
    assert saved.meta.updated_at.year >= 2026
    assert [row.updated_by for row in await revision_rows(db)] == [None, "admin:usr_a"]


async def test_a_failed_file_write_leaves_no_history_row(
    db: AsyncSession, tmp_path: Path, config: IntelligenceConfig
) -> None:
    """ファイル書き込みが失敗したら履歴も残さない。

    「履歴にはあるが実体が無い revision」を作ると `get_pinned()` が実在しない
    config を返す。ファイルが正なので、こちらを優先して rollback する。
    """

    class FailingStore(ArtifactStore):
        def write_text(self, path: Path, text: str) -> None:
            raise OSError("disk full")

    repo = ConfigRepository(
        db, FailingStore(root=tmp_path / "artifacts", tz=JST), tz=JST
    )

    with pytest.raises(OSError, match="disk full"):
        await repo.create_initial(config)

    assert not repo.exists()
    assert await revision_rows(db) == []


async def test_a_revision_already_in_the_history_is_reported_clearly(
    repo: ConfigRepository, db: AsyncSession, config: IntelligenceConfig
) -> None:
    """ファイルを手で戻した等の不整合を、主キー衝突の 500 にせず理由付きで止める。"""
    await repo.create_initial(config)
    await repo.save(repo.load(), base_revision=1, updated_by="admin:usr_a")

    # revision 2 の履歴を残したまま、ファイルだけ revision 1 に巻き戻す。
    rolled_back = repo.load().model_copy(
        update={"meta": repo.load().meta.model_copy(update={"revision": 1})}
    )
    repo.path.write_text(
        json.dumps(rolled_back.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ConfigRevisionAlreadyRecordedError) as excinfo:
        await repo.save(rolled_back, base_revision=1, updated_by="admin:usr_a")
    assert excinfo.value.revision == 2
    assert [row.revision for row in await revision_rows(db)] == [1, 2]


# --- ピン留め（実行中ジョブの固定参照。設計書 §6.3・§14）-------------------


async def test_a_pinned_revision_does_not_change_when_the_config_is_saved(
    repo: ConfigRepository, config: IntelligenceConfig
) -> None:
    """実行中に admin が保存しても、ジョブが見る基準は切り替わらない。"""
    await repo.create_initial(config)
    pinned_revision = repo.load().meta.revision

    current = repo.load()
    await repo.save(
        current.model_copy(
            update={
                "tunable_thresholds": current.tunable_thresholds.model_copy(
                    update={"min_total_score_to_publish": 50}
                )
            }
        ),
        base_revision=pinned_revision,
        updated_by="admin:usr_a",
    )

    pinned = await repo.get_pinned(pinned_revision)
    assert pinned.meta.revision == 1
    assert pinned.tunable_thresholds.min_total_score_to_publish == 60
    # 現行はもう変わっている。
    assert repo.load().tunable_thresholds.min_total_score_to_publish == 50


async def test_pinning_an_unknown_revision_says_so(repo: ConfigRepository) -> None:
    with pytest.raises(ConfigRevisionNotFoundError) as excinfo:
        await repo.get_pinned(7)
    assert excinfo.value.revision == 7


async def test_a_pinned_snapshot_round_trips_the_whole_config(
    repo: ConfigRepository, config: IntelligenceConfig
) -> None:
    """スナップショットは config 全体。ピン留めから読み直しても同じ内容。"""
    saved = await repo.create_initial(config)
    assert (await repo.get_pinned(1)).model_dump(mode="json") == saved.model_dump(
        mode="json"
    )


# --- 改訂履歴の一覧（GET /config/history。設計書 §3.3）---------------------


async def test_history_is_newest_first_and_limitable(
    repo: ConfigRepository, config: IntelligenceConfig
) -> None:
    await repo.create_initial(config)
    for base in (1, 2):
        current = repo.load()
        await repo.save(
            current.model_copy(
                update={
                    "tunable_thresholds": current.tunable_thresholds.model_copy(
                        update={"min_total_score_to_publish": 60 - base}
                    )
                }
            ),
            base_revision=base,
            updated_by=f"admin:usr_{base}",
        )

    items = await repo.list_revisions()
    assert [item.revision for item in items] == [3, 2, 1]
    assert [item.updated_by for item in items] == ["admin:usr_2", "admin:usr_1", None]
    assert [item.revision for item in await repo.list_revisions(limit=2)] == [3, 2]


async def test_history_items_do_not_carry_the_config_itself(
    repo: ConfigRepository, config: IntelligenceConfig
) -> None:
    """一覧は §3.3 の4項目だけ。config の中身を返す経路は `get_pinned` に絞る。"""
    await repo.create_initial(config)

    (item,) = await repo.list_revisions()
    assert isinstance(item, RevisionSummary)
    assert not hasattr(item, "config_snapshot")
    assert set(vars(item)) == {"revision", "updated_at", "updated_by", "diff_summary"}


# --- 差分（履歴の diff_summary ／ 監査ログの diff）-------------------------


def test_scalar_lists_are_compared_as_a_whole(config: IntelligenceConfig) -> None:
    """`enums.industry` を要素ごとに展開しない（1件挿入で全要素が変更に見える）。"""
    flat = flatten_config(config)

    assert isinstance(flat["enums.industry"], list)
    assert isinstance(flat["scoring_axes.0.bands"], list)
    # オブジェクトの配列は件数固定（7/10/6/13）なので添字で展開する。
    assert flat["scoring_axes.0.id"] == "customer_relevance"
    assert "information_categories.6.priority" in flat


def test_a_diff_names_the_axis_it_belongs_to(config: IntelligenceConfig) -> None:
    changed = with_weight_sum(config, delta=+5)

    diff = diff_configs(config, changed)
    assert diff == {
        "scoring_axes.0.weight": {"before": 25, "after": 30},
    }
    assert summarize_diff(diff) == "scoring_axes.0.weight 25→30"


def test_meta_changes_are_left_out_of_the_diff(config: IntelligenceConfig) -> None:
    """revision / updated_at / updated_by は列として別に持つ。差分には出さない。"""
    stamped = config.model_copy(
        update={
            "meta": config.meta.model_copy(
                update={
                    "revision": 9,
                    "updated_by": "admin:usr_a",
                    "updated_at": datetime(2030, 1, 1, tzinfo=JST),
                }
            )
        }
    )

    assert diff_configs(config, stamped) == {}
    assert summarize_diff(diff_configs(config, stamped)) is None


def test_long_diffs_are_folded(config: IntelligenceConfig) -> None:
    """履歴一覧の1行に収まるよう打ち切る。落とした件数は隠さない。"""
    rules = [
        rule.model_copy(update={"enabled": False}) for rule in config.exclusion_rules
    ]
    changed = config.model_copy(update={"exclusion_rules": rules})

    summary = summarize_diff(diff_configs(config, changed), max_entries=2)
    assert summary is not None
    assert summary.startswith("exclusion_rules.0.enabled true→false")
    assert summary.endswith("他11件")


def test_summaries_keep_japanese_readable(config: IntelligenceConfig) -> None:
    """日本語をエスケープしない（履歴一覧はそのまま人が読む）。"""
    changed = config.model_copy(
        update={
            "tunable_thresholds": config.tunable_thresholds.model_copy(
                update={
                    "weekly": config.tunable_thresholds.weekly.model_copy(
                        update={"target_industries": ["製造"]}
                    )
                }
            )
        }
    )

    summary = summarize_diff(diff_configs(config, changed))
    assert summary == (
        'tunable_thresholds.weekly.target_industries ["不動産"]→["製造"]'
    )
