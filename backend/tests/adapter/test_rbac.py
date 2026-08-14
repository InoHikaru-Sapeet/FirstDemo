"""権限マトリクス（T-09。設計書 §4.2 ／ 仕様書 §6.2）。

**このファイルの役割は2つに分かれている。両方揃って初めて認可を保証できる。**

1. **マトリクスの中身が §6.2 と一致しているか**（`PERMISSION_MATRIX` を仕様書の
   表と1:1で突き合わせる）。ここは HTTP を通さない純粋な定数比較で、
   **未実装のエンドポイント（`GET /reports` / `POST /run`）の行も対象**にする。
2. **HTTP 層が本当にマトリクスどおり弾いているか**（実装済みエンドポイントを
   全ロール＋未認証で叩く）。期待値は `authorize()` から導くので、ここは
   「定数の内容」ではなく **「配線されているか」** を検証している。

1 と 2 を分けているのは、片方だけだと穴が残るため。1 だけならマトリクスが正しくても
ルーターが参照していないかもしれず、2 だけなら「マトリクスもコードも同じように
間違っている」を検出できない。

⚠️ **`GET /reports/{period}` と `POST /run/{type}` の HTTP テストは無い**
（ルーターが T-27 で未実装のため）。定数としては 1 の対象に含めてある。
**T-27 でルーターを作ったら 2 の `REQUESTS` へ2行足すこと**（TASKS.md T-27）。

なお、各ルーター固有の認可要件（config の存在を 403 の差で悟らせない等）は
`test_config_router.py` / `test_users_router.py` が引き続き担当する。ここは
「全エンドポイント × 全ロール」の網羅に徹し、成功時のステータスや本文は見ない。
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.database.base import Base
from adapter.database.models.user import User
from adapter.http.fastapi.auth.dependencies import get_db_session, require_admin
from adapter.http.fastapi.auth.rbac import (
    ADMIN_ONLY_OPERATIONS,
    PERMISSION_MATRIX,
    PUBLIC_OPERATIONS,
    ROUTE_OPERATIONS,
    Decision,
    Operation,
    Outcome,
    authorize,
    resolve_operation,
    roles_allowed_over_http,
)
from adapter.http.fastapi.main import app
from adapter.http.fastapi.routers import all_routers
from config import get_settings
from enterprise.entities.principal import Principal, Role
from enterprise.services.password import hash_password
from enterprise.services.service_token import hash_service_token

PASSWORD = "correct horse battery staple"
SERVICE_TOKEN = "service-token-for-tests"

EMAILS = {
    Role.ADMIN: "admin@sapeet.com",
    Role.EDITOR: "editor@sapeet.com",
    Role.VIEWER: "viewer@sapeet.com",
}

# 呼び出し元の5通り。`system` はサービストークン、`anonymous` は資格情報なし。
CALLERS = ["admin", "editor", "viewer", "system", "anonymous"]


# =============================================================================
# 1. マトリクスの中身（仕様書 §6.2 との1:1突き合わせ）
# =============================================================================

ALLOW = Decision.ALLOW
DENY = Decision.DENY
INTERNAL_ONLY = Decision.INTERNAL_ONLY

# 仕様書 §6.2「権限マトリクス（API観点）」の表をそのまま書き写したもの。
# | 操作 | admin | editor | viewer | system |
# | `GET /config` | ○ | 403 | 403 | 内部のみ |
# | `PUT /config`（パラメータ更新） | ○ | 403 | 403 | × |
# | `GET /reports/{period}`（HTML/一覧） | ○ | ○ | ○ | ○ |
# | `POST /run/{weekly|monthly}`（実行） | ○ | ○ | 403 | ○ |
# | `GET /config/history`（改訂履歴） | ○ | 403 | 403 | × |
SPEC_6_2: dict[Operation, tuple[Decision, Decision, Decision, Decision]] = {
    Operation.GET_CONFIG: (ALLOW, DENY, DENY, INTERNAL_ONLY),
    Operation.PUT_CONFIG: (ALLOW, DENY, DENY, DENY),
    Operation.GET_REPORTS: (ALLOW, ALLOW, ALLOW, ALLOW),
    Operation.POST_RUN: (ALLOW, ALLOW, DENY, ALLOW),
    Operation.GET_CONFIG_HISTORY: (ALLOW, DENY, DENY, DENY),
}

# 設計書 §3.2 の追加行（§6.2 に列挙が無い設計時追加分。認可根拠は §3.4）。
# | `POST /config/dry-run` | 202 | 403 | 403 | × |
DESIGN_3_2_ADDITION: dict[Operation, tuple[Decision, Decision, Decision, Decision]] = {
    Operation.POST_CONFIG_DRY_RUN: (ALLOW, DENY, DENY, DENY),
}

# TASKS.md T-09「認証系エンドポイントをマトリクスへ追加」（2026-08-13 の方針変更分）。
TASKS_T09_AUTH_ROWS: dict[Operation, tuple[Decision, Decision, Decision, Decision]] = {
    Operation.POST_AUTH_REGISTER: (ALLOW, ALLOW, ALLOW, ALLOW),
    Operation.POST_AUTH_LOGIN: (ALLOW, ALLOW, ALLOW, ALLOW),
    Operation.POST_AUTH_LOGOUT: (ALLOW, ALLOW, ALLOW, ALLOW),
    Operation.GET_AUTH_ME: (ALLOW, ALLOW, ALLOW, ALLOW),
    Operation.POST_AUTH_PASSWORD: (ALLOW, ALLOW, ALLOW, ALLOW),
    # ⚠️ `/users` は admin のみ。**`system` も 403**（T-42）。
    Operation.GET_USERS: (ALLOW, DENY, DENY, DENY),
    Operation.PATCH_USER_ROLE: (ALLOW, DENY, DENY, DENY),
    Operation.PATCH_USER_STATUS: (ALLOW, DENY, DENY, DENY),
}

ALL_EXPECTED_ROWS = SPEC_6_2 | DESIGN_3_2_ADDITION | TASKS_T09_AUTH_ROWS


def actual_row(operation: Operation) -> tuple[Decision, Decision, Decision, Decision]:
    row = PERMISSION_MATRIX[operation]
    return (row[Role.ADMIN], row[Role.EDITOR], row[Role.VIEWER], row[Role.SYSTEM])


@pytest.mark.parametrize("operation", sorted(SPEC_6_2, key=lambda op: op.value))
def test_the_matrix_matches_spec_6_2(operation: Operation) -> None:
    """⚠️ **仕様書 §6.2 の確定値。ここを緩めない。**

    未実装（`GET /reports` / `POST /run`）の行も検査対象に含める。ルーターが
    無くても、マトリクスは §6.2 を写したものとして正しくなければならない。
    """
    assert actual_row(operation) == SPEC_6_2[operation]


@pytest.mark.parametrize(
    "operation", sorted(DESIGN_3_2_ADDITION, key=lambda op: op.value)
)
def test_the_dry_run_row_matches_design_3_2(operation: Operation) -> None:
    """dry-run は run ファミリではなく **config ファミリ**（設計書 §3.4）。"""
    assert actual_row(operation) == DESIGN_3_2_ADDITION[operation]


@pytest.mark.parametrize(
    "operation", sorted(TASKS_T09_AUTH_ROWS, key=lambda op: op.value)
)
def test_the_auth_and_user_rows_match_tasks_t09(operation: Operation) -> None:
    """2026-08-13 の方針変更で増えた行（TASKS.md T-09）。"""
    assert actual_row(operation) == TASKS_T09_AUTH_ROWS[operation]


def test_the_matrix_has_no_extra_or_missing_operations() -> None:
    """行の増減を検出する。**上の表に無い行を勝手に足させない。**"""
    assert set(PERMISSION_MATRIX) == set(Operation)
    assert set(PERMISSION_MATRIX) == set(ALL_EXPECTED_ROWS)


@pytest.mark.parametrize("operation", sorted(Operation, key=lambda op: op.value))
def test_every_operation_covers_every_role(operation: Operation) -> None:
    """⚠️ 4ロールすべてに明示の升目があること。

    「書き忘れたロールが黙って許可（または拒否）になる」経路を作らない。
    """
    assert set(PERMISSION_MATRIX[operation]) == set(Role)


# =============================================================================
# 1-b. マトリクスから導かれる判定（`authorize()`）
# =============================================================================


def test_internal_only_is_not_allowed_over_http() -> None:
    """⚠️ **`system` は HTTP から config を読めない**（403）。

    §6.2 は `GET /config` × `system` を「内部のみ」とし、§4.2 の擬似コードは
    `internal_only and caller.is_internal()` を許可としているが、その「内部」は
    パイプラインがファイルを直接読む経路のこと。**HTTP は内部ではない**
    （設計書 §3.1「system は内部読込のみで外部レスポンス経路を持たない」）。

    ここが `ALLOWED` に変わると、サービストークンを持つ cron が config を
    読めてしまい、「admin 以外に存在も中身も返さない」（仕様書 §2）が崩れる。
    """
    assert PERMISSION_MATRIX[Operation.GET_CONFIG][Role.SYSTEM] is INTERNAL_ONLY
    assert authorize(Operation.GET_CONFIG, Role.SYSTEM) is Outcome.FORBIDDEN
    assert Role.SYSTEM not in roles_allowed_over_http(Operation.GET_CONFIG)


def test_the_internal_only_marker_is_preserved_in_the_matrix() -> None:
    """`internal_only` を `deny` へ丸めない。

    HTTP での結果は同じ 403 だが、§6.2 が「×」と「内部のみ」を書き分けている
    以上、マトリクスも区別を保つ（将来この差が意味を持つのは、パイプラインが
    config を読む内部経路を明文化するとき）。
    """
    internal_only_cells = {
        (operation, role)
        for operation, row in PERMISSION_MATRIX.items()
        for role, decision in row.items()
        if decision is INTERNAL_ONLY
    }
    assert internal_only_cells == {(Operation.GET_CONFIG, Role.SYSTEM)}


def test_unauthenticated_is_reported_separately_from_forbidden() -> None:
    """⚠️ 401 と 403 を混ぜない（フロントの出し分けが崩れる。T-43）。"""
    assert authorize(Operation.GET_CONFIG, None) is Outcome.UNAUTHENTICATED
    assert authorize(Operation.GET_CONFIG, Role.VIEWER) is Outcome.FORBIDDEN
    assert authorize(Operation.GET_CONFIG, Role.ADMIN) is Outcome.ALLOWED


def test_public_operations_do_not_require_authentication() -> None:
    """ログインの入口を認証で塞がない（塞ぐと誰もログインできない）。"""
    assert PUBLIC_OPERATIONS == {
        Operation.POST_AUTH_REGISTER,
        Operation.POST_AUTH_LOGIN,
        Operation.POST_AUTH_LOGOUT,
    }
    for operation in PUBLIC_OPERATIONS:
        assert authorize(operation, None) is Outcome.ALLOWED


def test_no_config_or_user_operation_is_public() -> None:
    """⚠️ public 集合に config / users が紛れ込んでいないこと。"""
    for operation in PUBLIC_OPERATIONS:
        assert operation.value.startswith(("POST /auth", "GET /auth"))


def test_admin_only_operations_are_derived_from_the_matrix() -> None:
    """`ADMIN_ONLY_OPERATIONS` はマトリクスから導出され、config と users を覆う。"""
    assert ADMIN_ONLY_OPERATIONS == {
        Operation.GET_CONFIG,
        Operation.PUT_CONFIG,
        Operation.GET_CONFIG_HISTORY,
        Operation.POST_CONFIG_DRY_RUN,
        Operation.GET_USERS,
        Operation.PATCH_USER_ROLE,
        Operation.PATCH_USER_STATUS,
    }
    for operation in ADMIN_ONLY_OPERATIONS:
        assert roles_allowed_over_http(operation) == {Role.ADMIN}


def test_an_unknown_route_resolves_to_no_operation() -> None:
    """マトリクスに行の無いルートは `None`。呼び出し側が fail-closed にする。"""
    assert resolve_operation("GET", "/nope") is None
    assert resolve_operation("DELETE", "/config") is None
    # メソッドの大文字小文字は問わない（`request.method` は大文字だが念のため）。
    assert resolve_operation("get", "/config") is Operation.GET_CONFIG


# =============================================================================
# 2-a. アプリの配線（ルートとマトリクスの対応漏れ）
# =============================================================================

# 認可の対象外。**データを返さない**死活監視用エンドポイントで、§6.2 にも行が無い。
EXEMPT_ROUTES = {("GET", "/healthz"), ("GET", "/readyz")}


def api_routes() -> list[tuple[str, str]]:
    """登録されている (メソッド, パステンプレート) の一覧。

    ⚠️ **`app.routes` ではなく `all_routers` から取る。** この FastAPI は
    `include_router()` したルーターを `_IncludedRouter` という内部オブジェクトで
    保持しており、`app.routes` を1階層見ても `APIRoute` が出てこない（実測）。
    内部構造に寄りかかるより、アプリ自身が登録に使っている `all_routers` を
    正として読むほうが壊れにくい。

    `main.py` が prefix 無しで include しているので、ルーター側の
    `APIRouter(prefix=...)` がそのまま最終パスになる。**その前提が崩れていない
    ことは `test_the_router_registry_matches_the_served_paths` が実際に配信されて
    いるパス（OpenAPI）と突き合わせて検査する。**
    """
    found: list[tuple[str, str]] = []
    for router in all_routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method, route.path))
    return found


def test_the_router_registry_matches_the_served_paths() -> None:
    """`all_routers` から読んだパスが、実際に配信されているパスと一致すること。

    `include_router(prefix=...)` が足されると両者がずれ、`ROUTE_OPERATIONS` の
    キーが実パスと合わなくなる（＝そのルートが常に 403 になる）。
    """
    served = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert set(api_routes()) == served


def test_every_route_is_covered_by_the_matrix() -> None:
    """⚠️ **新しいエンドポイントを認可なしで生やさせない。**

    ルートを足してマトリクスへ登録し忘れると、`require_permission` は
    fail-closed で 403 を返す（安全側だが原因が分かりにくい）。ここで
    「登録し忘れ」を名指しで落とす。
    """
    uncovered = [
        route
        for route in api_routes()
        if route not in ROUTE_OPERATIONS and route not in EXEMPT_ROUTES
    ]
    assert uncovered == [], (
        f"権限マトリクスに行が無いルート: {uncovered}。"
        "rbac.py の Operation / PERMISSION_MATRIX / ROUTE_OPERATIONS へ追加すること。"
    )


def test_the_route_table_points_only_at_real_routes() -> None:
    """逆向き：`ROUTE_OPERATIONS` に実在しないルートが残っていないこと。

    パスの綴り違い（prefix の付け忘れ等）はこれで落ちる。綴りを間違えると
    `resolve_operation` が `None` を返し、そのルートが常に 403 になる。
    """
    registered = set(api_routes())
    stale = [route for route in ROUTE_OPERATIONS if route not in registered]
    assert stale == []


def dependency_calls(dependant: Dependant) -> set[Any]:
    """依存ツリーを再帰的にたどって、使われている依存関数を集める。"""
    calls: set[Any] = set()
    for sub in dependant.dependencies:
        if sub.call is not None:
            calls.add(sub.call)
        calls |= dependency_calls(sub)
    return calls


def routes_using_require_admin() -> set[tuple[str, str]]:
    using: set[tuple[str, str]] = set()
    for router in all_routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            if require_admin not in dependency_calls(route.dependant):
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                using.add((method, route.path))
    return using


def test_require_admin_is_only_used_on_admin_only_routes() -> None:
    """⚠️ `require_admin` の名前と実体が食い違っていないこと。

    マトリクス上 admin 限定でないオペレーションにこれを付けると、名前は
    「admin 限定」なのに実際は別のロールも通る、という読み違いが生まれる。
    """
    for method, path in routes_using_require_admin():
        operation = resolve_operation(method, path)
        assert operation is not None, f"{method} {path} がマトリクス未登録"
        assert operation in ADMIN_ONLY_OPERATIONS, (
            f"{method} {path} は admin 限定ではないのに require_admin を使っている"
        )


def test_every_implemented_admin_only_route_enforces_it() -> None:
    """⚠️ **逆向きが本命。** admin 限定のはずのルートで依存を付け忘れていないか。

    付け忘れると誰でも config やユーザー一覧を読めてしまう。マトリクスに
    admin 限定と書いてあるだけでは何も守れない。
    """
    expected = {
        (method, path)
        for (method, path), operation in ROUTE_OPERATIONS.items()
        if operation in ADMIN_ONLY_OPERATIONS
    }
    assert expected == routes_using_require_admin()


# =============================================================================
# 2-b. HTTP 網羅（実装済みエンドポイント × 全ロール ＋ 未認証）
# =============================================================================


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SERVICE_TOKEN_HASH", hash_service_token(SERVICE_TOKEN))
    get_settings.cache_clear()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'rbac.db'}", poolclass=NullPool
    )

    async def create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db
    with TestClient(app) as test_client:
        test_client._maker = maker  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def seed_user(client: TestClient, email: str, role: Role) -> str:
    async def insert() -> str:
        now = datetime(2026, 8, 1, tzinfo=UTC)
        async with client._maker() as session:  # type: ignore[attr-defined]
            user = User(
                user_id=f"usr_{role.value}",
                email=email,
                display_name="テスト 花子",
                password_hash=hash_password(PASSWORD),
                role=role,
                is_active=True,
                created_at=now,
                updated_at=now,
                password_updated_at=now,
                failed_login_attempts=0,
                locked_until=None,
            )
            session.add(user)
            await session.commit()
            return user.user_id

    return asyncio.run(insert())


def authenticate_as(client: TestClient, caller: str) -> None:
    """呼び出し元をその資格情報の状態にする。

    `system` だけはサービストークン（`Authorization: Bearer`）で、Cookie を持たない
    （T-41。`users` に行を持たないため）。
    """
    if caller == "anonymous":
        return
    if caller == "system":
        client.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"
        return
    role = Role(caller)
    seed_user(client, EMAILS[role], role)
    response = client.post(
        "/auth/login", json={"email": EMAILS[role], "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


@dataclass(frozen=True)
class Call:
    """網羅テストが投げる1リクエスト。"""

    operation: Operation
    method: str
    url: str
    body: dict[str, Any] | None = None

    def send(self, client: TestClient) -> int:
        response = client.request(self.method, self.url, json=self.body)
        return response.status_code


# 実装済みエンドポイントのみ。⚠️ **T-27 で `/reports` `/run`、T-29 で
# `/config/dry-run` を実装したらここへ足すこと。**
REQUESTS = [
    Call(Operation.GET_CONFIG, "GET", "/config"),
    Call(Operation.GET_CONFIG_HISTORY, "GET", "/config/history"),
    Call(Operation.PUT_CONFIG, "PUT", "/config", {"base_revision": 1, "patch": {}}),
    Call(Operation.GET_USERS, "GET", "/users"),
    Call(
        Operation.PATCH_USER_ROLE,
        "PATCH",
        "/users/usr_target/role",
        {"role": "editor"},
    ),
    Call(
        Operation.PATCH_USER_STATUS,
        "PATCH",
        "/users/usr_target/status",
        {"is_active": False},
    ),
    Call(Operation.GET_AUTH_ME, "GET", "/auth/me"),
    Call(
        Operation.POST_AUTH_PASSWORD,
        "POST",
        "/auth/password",
        {"current_password": PASSWORD, "new_password": "a brand new passphrase"},
    ),
    Call(
        Operation.POST_AUTH_REGISTER,
        "POST",
        "/auth/register",
        {
            "email": "newcomer@sapeet.com",
            "display_name": "新入 太郎",
            "password": PASSWORD,
        },
    ),
    Call(
        Operation.POST_AUTH_LOGIN,
        "POST",
        "/auth/login",
        {"email": "nobody@sapeet.com", "password": PASSWORD},
    ),
    Call(Operation.POST_AUTH_LOGOUT, "POST", "/auth/logout"),
]


def test_the_http_sweep_covers_every_implemented_route() -> None:
    """⚠️ `REQUESTS` の取りこぼしを防ぐ。

    ルートを足してこの表に入れ忘れると、網羅テストを名乗ったまま穴が空く。
    """
    swept = {call.operation for call in REQUESTS}
    implemented = set(ROUTE_OPERATIONS.values())
    assert swept == implemented


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("call", REQUESTS, ids=lambda c: c.operation.value)
def test_the_http_layer_enforces_the_matrix(
    client: TestClient, call: Call, caller: str
) -> None:
    """実装済みエンドポイント × 全ロール ＋ 未認証の網羅。

    期待値は `authorize()` から導く。**ここが検証しているのは「マトリクスの中身」
    ではなく「HTTP 層がマトリクスを参照しているか（配線）」。** 中身の正しさは
    このファイル前半が §6.2 と突き合わせて担保している。

    許可された呼び出しについては **403 でないこと**だけを見る。成功時の
    ステータス（200 / 201 / 204 / 404 / 409 / 422）は各ルーターのテストの担当で、
    ここで縛ると認可と無関係な理由でテストが壊れる。
    """
    authenticate_as(client, caller)
    role = None if caller == "anonymous" else Role(caller)
    outcome = authorize(call.operation, role)

    status_code = call.send(client)

    if outcome is Outcome.UNAUTHENTICATED:
        assert status_code == 401, f"{caller} {call.method} {call.url}"
    elif outcome is Outcome.FORBIDDEN:
        assert status_code == 403, f"{caller} {call.method} {call.url}"
    else:
        assert status_code != 403, f"{caller} {call.method} {call.url}"


@pytest.mark.parametrize("call", REQUESTS, ids=lambda c: c.operation.value)
def test_anonymous_gets_401_never_403_on_protected_routes(
    client: TestClient, call: Call
) -> None:
    """⚠️ **401 / 403 の統一。** 未認証には必ず 401 を返す（403 と混ぜない）。

    混ぜるとフロントが「ログインへ誘導する」か「権限不足を表示する」かを
    判断できない（T-43 の `RequireAuth` と `QueryClient` の 401 処理）。
    public なオペレーションだけは未認証でも通す。
    """
    status_code = call.send(client)

    if call.operation in PUBLIC_OPERATIONS:
        assert status_code != 401 or call.operation is Operation.POST_AUTH_LOGIN
        assert status_code != 403
    else:
        assert status_code == 401


# --- ★ 調査で判明していた穴（TASKS.md T-09 備考）-----------------------------


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "/users", None),
        ("PATCH", "/users/usr_target/role", {"role": "editor"}),
        ("PATCH", "/users/usr_target/status", {"is_active": False}),
    ],
)
def test_a_service_token_cannot_reach_the_user_management_api(
    client: TestClient, method: str, url: str, body: dict[str, Any] | None
) -> None:
    """⚠️ **`system`（cron）は `/users` を叩けない（403）。**

    T-09 の完了条件に「`system` も 403」と明記されているが、2026-08-14 の調査
    時点で `/users` をサービストークンで叩くテストが1件も無かった（TASKS.md
    T-09 備考）。ユーザー管理は人が行う操作で、§6.2 の `internal_only`
    （パイプラインの内部読込）にも当てはまらない。

    ここが通ると、cron のトークンを持っているだけで**任意のユーザーを admin へ
    昇格できる**。認可の中で最も被害の大きい経路なので個別に固定する。
    """
    client.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"

    response = client.request(method, url, json=body)

    assert response.status_code == 403


def test_a_service_token_is_authenticated_but_still_forbidden(
    client: TestClient,
) -> None:
    """`system` の 403 が「認証できていない」せいではないことを示す。

    サービストークン自体は有効（別経路では通る）。それでも `/users` では
    403 になる＝**認可で弾いている**、という切り分け。
    """
    client.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"

    # 認証は通っている（未認証なら 401 になるはず）。
    assert client.get("/users").status_code == 403
    # 不正なトークンなら 401（認証段階で落ちる）。
    client.headers["Authorization"] = "Bearer wrong-token"
    assert client.get("/users").status_code == 401


# --- 認証系の扱い ------------------------------------------------------------


def test_the_authenticated_auth_routes_allow_every_role() -> None:
    """`require_principal`（ロールを見ない）とマトリクスが一致していること。

    `GET /auth/me` / `POST /auth/password` は4ロールすべて `allow` なので、
    「認証済みなら誰でも」という `require_principal` の判定と結果が同じになる。
    ここがずれたら、これらのルートも `require_permission` へ寄せる必要がある。
    """
    for operation in (Operation.GET_AUTH_ME, Operation.POST_AUTH_PASSWORD):
        assert roles_allowed_over_http(operation) == set(Role)
        assert operation not in PUBLIC_OPERATIONS


def test_logout_stays_reachable_without_a_session(client: TestClient) -> None:
    """⚠️ ログアウトはべき等（T-40 の完了条件）。未ログインでも 204。

    TASKS.md T-09 は logout を「認証済みの全ロール可」に分類しているが、
    実装・テストとして確定しているのは T-40 のべき等要件なので public 扱いにした
    （`rbac.py` の `PUBLIC_OPERATIONS` のコメント参照）。
    """
    assert client.post("/auth/logout").status_code == 204


def test_the_denial_message_reveals_nothing(client: TestClient) -> None:
    """403 の本文が config の存在・構造をほのめかさないこと（仕様書 §2・§6.1）。"""
    authenticate_as(client, "viewer")

    response = client.get("/config")

    assert response.status_code == 403
    body = response.text
    for leaked in ("revision", "scoring_axes", "exclusion_rules", "config.json"):
        assert leaked not in body


def test_the_principal_is_the_only_input_to_authorization() -> None:
    """認可はロールとユーザ識別子だけで決まる（TASKS.md §1.1「認可」）。

    `authorize()` の引数がロールだけであることが、認証方式（ID/PW / SSO /
    サービストークン）を差し替えても認可が無変更で済むことの担保。
    """
    for role in Role:
        principal = Principal(subject="whoever", role=role)
        assert authorize(Operation.GET_USERS, principal.role) is authorize(
            Operation.GET_USERS, role
        )
