"""登録するルーターの集約。main.py はこの all_routers を回すだけ。"""

from adapter.http.fastapi.routers import health, readiness

all_routers = [
    health.router,
    readiness.router,
]
