"""レディネスチェック。DB に到達できるか（readiness）を返す。"""

from fastapi import APIRouter
from sqlalchemy import text

from adapter.database.database import db_manager

router = APIRouter(tags=["health"])


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    async with db_manager.session() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ready"}
