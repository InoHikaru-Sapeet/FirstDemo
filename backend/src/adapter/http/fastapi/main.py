"""FastAPI アプリのエントリポイント。ルーター登録のみを担う薄い層。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

for router in all_routers:
    app.include_router(router)

logger.info("Application initialized: %s", settings.app_name)
