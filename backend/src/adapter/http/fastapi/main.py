"""FastAPI アプリのエントリポイント。ルーター登録のみを担う薄い層。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from adapter.http.fastapi.auth.csrf import build_csrf_middleware
from adapter.http.fastapi.routers import all_routers
from common.logger import logger
from config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF 対策（T-40）。Cookie 認証なので、更新系は Origin も検証する。
# ⚠️ cors_allowed_origins が既定の `*` のままだと素通りする。本番では設定すること。
app.middleware("http")(build_csrf_middleware(settings.cors_origins))

for router in all_routers:
    app.include_router(router)

logger.info("Application initialized: %s", settings.app_name)
