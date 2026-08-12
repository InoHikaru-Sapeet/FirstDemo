"""アプリ共通のロガー。ログは英語で、処理の開始・完了がわかるように出す。"""

import logging

from config import get_settings

_settings = get_settings()

logging.basicConfig(
    level=getattr(logging, _settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(_settings.app_name)
