"""登録するルーターの集約。main.py はこの all_routers を回すだけ。"""

from adapter.http.fastapi.routers import auth, health, readiness

all_routers = [
    health.router,
    readiness.router,
    auth.router,
]
