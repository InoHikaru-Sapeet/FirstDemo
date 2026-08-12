"""ヘルスチェック。アプリが生きているか（liveness）だけを返す。"""

from fastapi import APIRouter

from application.usecases.health import HealthUsecase

router = APIRouter(tags=["health"])
_usecase = HealthUsecase()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return _usecase.check()
