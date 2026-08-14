"""Config API（T-12 / T-13。設計書 §3.2・§3.3 ／ 仕様書 §6.1・§6.2・§7.4）。

`GET /config` / `GET /config/history` / `PUT /config`。**すべて admin のみ**。

---

⚠️ **最重要要件（仕様書 §2 重要要件・§6.1、顧客指定）**

`config.json` の**表示・編集は admin のみ**で、editor / viewer には
**存在も中身も露出しない**。実体は API 側の 403 であり、フロントの
非表示（T-32）は補助にすぎない。この方針から次が導かれる:

1. **認可はハンドラの手前で終わらせる。** `require_admin` は依存関係として
   解決され、**ボディの検証より先に**走る。だから非 admin の `PUT /config` は
   patch の中身に関係なく 403 で、「そのフィールドは編集できません」といった
   config の構造をほのめかす 422 を返さない。
2. **403 の応答は config の状態に依存させない。** `config.json` が有ろうと
   無かろうと、非 admin への応答は**同一のステータスと本文**にする。
   ここが状態依存になると（例：未作成なら 404、作成済みなら 403）、
   403/404 の出し分けだけで config の存在を推定されてしまう。
   ハンドラへ入る前に弾く＝ファイルを一切読まない、が担保になる。
3. **エラー本文に config 由来の値を混ぜない。** 403 の文言は権限の話だけを
   する（revision も項目名も enum 値も出さない）。

⚠️ **`system`（cron）も 403。** §6.2 の `GET /config` は system が「内部のみ」＝
パイプラインがファイルを直接読む経路のことで、**外部レスポンス経路は持たない**
（設計書 §3.1）。HTTP でこの API を叩ける相手は admin だけ。

⚠️ **`GET /openapi.json` は未認証で到達でき、`config` のレスポンススキーマ
（フィールド名と enum の日本語確定値）が載る。** T-12 の完了条件が
「OpenAPI にレスポンススキーマが出る（T-31 の型生成の入力）」を求めているため
意図的にそうしているが、**構造は非 admin にも見える**ということでもある。
露出するのはスキーマ（器）であって、revision・weight・しきい値といった
**運用中の値は含まれない**。値の秘匿とは別の論点として、`/openapi.json` と
`/docs` を admin 限定にするか否かは T-38 で判断すること。
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.config_repository import (
    ConfigNotFoundError,
    ConfigRepository,
    ConfigRevisionConflictError,
)
from adapter.http.fastapi.auth.dependencies import get_db_session, require_admin
from application.usecases.update_config import (
    ConfigPatchError,
    UpdateConfigUsecase,
)
from enterprise.entities.config import IntelligenceConfig
from enterprise.entities.config_validation import ConfigValidationError
from enterprise.entities.principal import Principal

router = APIRouter(prefix="/config", tags=["config"])


# --- DI ----------------------------------------------------------------------
# ⚠️ 認証系の DI（`auth/dependencies.py`）とは別に置く。config の置き場は
# 認証の関心事ではない。テストは `get_db_session` を差し替えればここも従う。


def get_config_repository(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConfigRepository:
    return ConfigRepository.from_settings(db)


def get_update_config_usecase(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    repo: Annotated[ConfigRepository, Depends(get_config_repository)],
) -> UpdateConfigUsecase:
    return UpdateConfigUsecase(db=db, repo=repo)


# --- I/O（設計書 §3.3）-------------------------------------------------------


class _StrictModel(BaseModel):
    """未知のキーを拒否する（余計なフィールドを黙って無視しない）。"""

    model_config = ConfigDict(extra="forbid")


class ConfigResponse(BaseModel):
    """`GET /config` → `{ "revision": N, "config": {...} }`。

    `config` に `IntelligenceConfig` を据えているので、OpenAPI に config 全体の
    スキーマが出る（T-31 の型生成の入力。モジュール冒頭の⚠️も参照）。
    """

    revision: int
    config: IntelligenceConfig


class RevisionItem(BaseModel):
    """改訂履歴の1件（設計書 §3.3 の items は**この4項目のみ**）。

    ⚠️ **config の中身を足さないこと。** 一覧に載せると、履歴が config の
    複製になり、admin 限定の経路を増やすだけで得がない。中身を返すのは
    `GET /config`（現行）と `get_pinned()`（実行時の固定参照）だけ。
    """

    revision: int
    updated_at: datetime
    updated_by: str | None
    diff_summary: str | None


class ConfigHistoryResponse(BaseModel):
    items: list[RevisionItem]


class UpdateConfigRequest(_StrictModel):
    """`PUT /config` のリクエスト（設計書 §3.3）。

    `patch` を自由形式の辞書で受けるのは、**許可リストの判定を1箇所
    （`application.usecases.update_config.EDITABLE_PATHS`）に集約する**ため。
    Pydantic のモデルで表現すると、許可判定が「型定義」と「§7.2 の表」に
    分かれ、422 の本文も FastAPI 既定の形（`detail: [...]`）になって
    T-05 の `issues` と揃わない。
    """

    base_revision: int = Field(ge=1)
    patch: dict[str, Any] = Field(
        examples=[
            {
                "scoring_axes": [{"id": "customer_relevance", "weight": 25}],
                "tunable_thresholds": {"min_total_score_to_publish": 62},
                "exclusion_rules": [{"no": 11, "enabled": False}],
            }
        ]
    )


class UpdateConfigResponse(BaseModel):
    revision: int
    updated_at: datetime | None
    updated_by: str | None


def _validation_failed(issues: list[Any]) -> HTTPException:
    """T-05 / patch 検査の違反を 422 にする（設計書 §3.3）。

    本文は `{"error":"validation_failed","issues":[{path,reason,code}]}`。
    プロジェクト共通の `detail` 封筒に載せる（T-40・T-42 と同じ形）。
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error": "validation_failed",
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
    )


# --- エンドポイント -----------------------------------------------------------


@router.get("")
async def get_config(
    _admin: Annotated[Principal, Depends(require_admin)],
    repo: Annotated[ConfigRepository, Depends(get_config_repository)],
) -> ConfigResponse:
    """現行 config を返す（**admin のみ**）。

    ⚠️ `_admin` は使わないが、**依存として宣言することが認可そのもの**。
    外すと誰でも config を読めるようになる。

    `config.json` が未作成なら 404（初期マイグレーション T-14 が未実行）。
    ⚠️ この 404 に到達できるのは admin だけ。非 admin は上の `require_admin` で
    403 になるため、404/403 の差から config の存在を推定されることはない。
    """
    try:
        config = repo.load()
    except ConfigNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "config_not_found", "message": str(exc)},
        ) from exc

    return ConfigResponse(revision=config.meta.revision, config=config)


@router.get("/history")
async def get_config_history(
    _admin: Annotated[Principal, Depends(require_admin)],
    repo: Annotated[ConfigRepository, Depends(get_config_repository)],
) -> ConfigHistoryResponse:
    """改訂履歴を新しい順に返す（**admin のみ**。設計書 §3.3）。

    `list_revisions()` は `config_snapshot` 列を SELECT しないので、
    この経路から config の中身は出ない（T-11）。
    """
    revisions = await repo.list_revisions()
    return ConfigHistoryResponse(
        items=[
            RevisionItem(
                revision=revision.revision,
                updated_at=revision.updated_at,
                updated_by=revision.updated_by,
                diff_summary=revision.diff_summary,
            )
            for revision in revisions
        ]
    )


@router.put("")
async def update_config(
    body: UpdateConfigRequest,
    admin: Annotated[Principal, Depends(require_admin)],
    usecase: Annotated[UpdateConfigUsecase, Depends(get_update_config_usecase)],
) -> UpdateConfigResponse:
    """config を更新する（**admin のみ**。設計書 §3.3・§4.3）。

    - `base_revision` 不一致 → **409** `{"error":"revision_conflict",
      "current_revision":N}`（楽観ロック。仕様書 §6.3）
    - §7.2 の編集可能パラメータ以外を含む patch → **422**
    - T-05 のクロスフィールド違反（配点合計100・しきい値の降順など）→ **422**。
      ⚠️ **自動補正はしない**（設計判断A）。拒否された場合 `config.json` は
      1バイトも変わらない
    - 成功 → **200** `{revision, updated_at, updated_by}`、`revision++`、
      **監査ログに diff を記録**（仕様書 §6.1）

    ⚠️ 非 admin は patch の中身に関わらず **403**。`require_admin` が
    ボディ検証より先に走るため、422 の項目名から config の構造を推測させない。
    """
    try:
        saved = await usecase.execute(
            admin, base_revision=body.base_revision, patch=body.patch
        )
    except ConfigNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "config_not_found", "message": str(exc)},
        ) from exc
    except ConfigRevisionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "revision_conflict",
                "current_revision": exc.current_revision,
                "message": str(exc),
            },
        ) from exc
    except (ConfigPatchError, ConfigValidationError) as exc:
        raise _validation_failed(exc.issues) from exc

    return UpdateConfigResponse(
        revision=saved.meta.revision,
        updated_at=saved.meta.updated_at,
        updated_by=saved.meta.updated_by,
    )
