"""登録するルーターの集約。main.py はこの all_routers を回すだけ。"""

from adapter.http.fastapi.routers import auth, config, health, readiness, users

all_routers = [
    health.router,
    readiness.router,
    auth.router,
    users.router,
    config.router,
]
