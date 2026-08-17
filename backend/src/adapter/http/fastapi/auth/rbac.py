"""権限マトリクス（T-09。設計書 §4.2 ／ 仕様書 §6.2）。

**認可判定の正はこのモジュール1つ。** ルーターやユースケースがロールを見て
直接分岐しないこと。判定の正が分かれると、マトリクスを直してもエンドポイントが
追随しない（T-42 着手時に `require_admin()` を先取りした際の反省。TASKS.md T-09）。

判定の入力は `Principal`（ロール＋ユーザ識別子）だけで、パスワード・セッション・
トークンといった**認証の実現方法を認可へ持ち込まない**（TASKS.md §1.1「認可」）。
認証方式を差し替えてもこのモジュールは無変更で済む。

---

⚠️ **`internal_only` は HTTP では通さない（403）。**

仕様書 §6.2 は `GET /config` × `system` を「内部のみ」とし、設計書 §4.2 の擬似コードは
`decision == "internal_only" and caller.is_internal()` を許可としている。
これを額面どおり `Principal.is_internal` で実装すると、**サービストークンを持つ cron が
`GET /config` を 200 で読めてしまう**。

設計書 §3.1 が「`system` は内部読込のみで**外部レスポンス経路を持たない**」と定めている
とおり、ここでいう「内部」とは**パイプラインが `ArtifactStore` 経由でファイルを直接読む
経路**のことで、**HTTP はその内部ではない**。したがって HTTP 層に到達した時点で
`internal_only` は許可になりえず、`deny` と同じ 403 に落ちる。

これは T-12 が実装しテストで固定済みの挙動でもある
（`test_config_router.py::test_only_an_admin_can_read_the_config` の `system` ケース）。
**`Principal.is_internal` をこの判定に使わないこと。** 使うと「config は admin 以外に
存在も中身も返さない」（仕様書 §2 重要要件・§6.1）が根本から崩れる。

---

⚠️ **未実装エンドポイントの行も定数には含める。**

マトリクスは §6.2 を**そのまま**写すのが役目なので、ルーターが無い行も定義し、
`ROUTE_OPERATIONS`（実ルートとの対応）にだけ載せない。**ルートを足すときに
対応を追加し、`tests/adapter/test_rbac.py` の網羅テストへケースを足すこと。**

（2026-08-17 T-27 実施）`GET /reports/{period}` と `POST /run/{type}` は
ルーターができたので `ROUTE_OPERATIONS` へ登録し、網羅テストの対象に入れた。

（2026-08-17 T-29 実施）`POST /config/dry-run` も登録し、**最後まで残っていた
未実装行が無くなった**。ドライラン明細のダウンロード
（`GET /config/dry-run/{dry_run_id}/result.xlsx`）は設計時にも列挙が無い追加分で、
**config ファミリと同じ行**にした（§3.4 の判断をそのまま延長したもの。中身は
「未保存の config を適用した結果」そのものなので、config より広くはできない）。
"""

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping

from enterprise.entities.principal import Role


class Decision(StrEnum):
    """マトリクスの升目（設計書 §4.2）。

    `INTERNAL_ONLY` は仕様書 §6.2 の「内部のみ」。**HTTP 層では許可にならない**
    （モジュール冒頭の警告を参照）。値として保持しているのは、§6.2 を情報を落とさず
    写すため＝「`deny` と書いてある行」と「`内部のみ` と書いてある行」を区別するため。
    """

    ALLOW = "allow"
    DENY = "deny"
    INTERNAL_ONLY = "internal_only"


class Outcome(StrEnum):
    """認可の結果。HTTP ステータスへの変換は呼び出し側（`dependencies.py`）が行う。

    ⚠️ **未認証（401）と権限なし（403）を混ぜない。** フロントが「ログインへ誘導する」
    のか「権限不足を表示する」のかを判断できなくなる（TASKS.md T-40・T-43）。
    """

    ALLOWED = "allowed"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"


class Operation(StrEnum):
    """認可の単位。値は仕様書 §6.2・設計書 §3.2 の「メソッド パス」表記に揃える。

    パスはルートのテンプレート（`{user_id}` 等をそのまま含む）。実ルートとの
    対応は `ROUTE_OPERATIONS` が持つ。
    """

    # --- config ファミリ（仕様書 §6.2 ／ 設計書 §3.2）------------------------
    GET_CONFIG = "GET /config"
    PUT_CONFIG = "PUT /config"
    GET_CONFIG_HISTORY = "GET /config/history"
    # §6.2 に列挙が無い設計時追加分。run ファミリではなく config ファミリへ
    # 寄せる判断の根拠は設計書 §3.4（dry-run は config 値とその適用挙動を露出する）。
    POST_CONFIG_DRY_RUN = "POST /config/dry-run"
    # T-29 の追加分（設計書 §3.2 にも行が無い）。ドライラン明細のダウンロード。
    GET_CONFIG_DRY_RUN_RESULT = "GET /config/dry-run/{dry_run_id}/result.xlsx"

    # --- run / reports ファミリ（仕様書 §6.2）--------------------------------
    GET_REPORTS = "GET /reports/{period}"
    POST_RUN = "POST /run/{type}"
    # §6.2 に列挙が無い設計時追加分（T-27）。理由は下の PERMISSION_MATRIX を参照。
    GET_RUN_JOB = "GET /run/{job_id}"
    GET_FILES = "GET /files/{filename}"

    # --- 認証（2026-08-13 の方針変更で増えた分。TASKS.md T-09）---------------
    POST_AUTH_REGISTER = "POST /auth/register"
    POST_AUTH_LOGIN = "POST /auth/login"
    POST_AUTH_LOGOUT = "POST /auth/logout"
    GET_AUTH_ME = "GET /auth/me"
    POST_AUTH_PASSWORD = "POST /auth/password"

    # --- ユーザー管理（T-42）------------------------------------------------
    GET_USERS = "GET /users"
    PATCH_USER_ROLE = "PATCH /users/{user_id}/role"
    PATCH_USER_STATUS = "PATCH /users/{user_id}/status"


_ALLOW = Decision.ALLOW
_DENY = Decision.DENY
_INTERNAL_ONLY = Decision.INTERNAL_ONLY


def _row(
    *, admin: Decision, editor: Decision, viewer: Decision, system: Decision
) -> Mapping[Role, Decision]:
    """マトリクスの1行。**4ロールすべてを必ず書く**（黙った既定値を作らない）。"""
    return MappingProxyType(
        {
            Role.ADMIN: admin,
            Role.EDITOR: editor,
            Role.VIEWER: viewer,
            Role.SYSTEM: system,
        }
    )


PERMISSION_MATRIX: Final[Mapping[Operation, Mapping[Role, Decision]]] = (
    MappingProxyType(
        {
            # === 仕様書 §6.2 をそのまま写した5行 ==============================
            # | 操作 | admin | editor | viewer | system |
            # | GET /config | ○ | 403 | 403 | 内部のみ |
            Operation.GET_CONFIG: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_INTERNAL_ONLY
            ),
            # | PUT /config | ○ | 403 | 403 | × |
            Operation.PUT_CONFIG: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
            # | GET /config/history | ○ | 403 | 403 | × |
            Operation.GET_CONFIG_HISTORY: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
            # | GET /reports/{period} | ○ | ○ | ○ | ○ |
            Operation.GET_REPORTS: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            # | POST /run/{weekly|monthly} | ○ | ○ | 403 | ○ |
            Operation.POST_RUN: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_DENY, system=_ALLOW
            ),
            # === 設計書 §3.2 の追加行（認可根拠は §3.4）=======================
            Operation.POST_CONFIG_DRY_RUN: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
            # === T-29 の追加行（§6.2 にも §3.2 にも列挙が無い）================
            # ⚠️ **明細のダウンロードは `POST /config/dry-run` と同じ行。**
            # ファイルの中身は「未保存の config を適用した結果」＝ config の値と
            # その適用挙動そのもの（§3.4 の根拠2）。実行を許していない相手に
            # 結果ファイルだけ配る意味は無く、逆に配れば §3.4 が塞いだ穴が
            # ファイル経由で開く。**`GET /files/{filename}`（全ロール可）には
            # 載せない**（`ArtifactStore.is_servable` が scratch を通さない）。
            # → §3.2 の表への追記が必要（T-38）。
            Operation.GET_CONFIG_DRY_RUN_RESULT: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
            # === T-27 の追加行（§6.2 にも §3.2 にも列挙が無い）================
            # ⚠️ **ジョブ状態の照会は `POST /run` と同じ行にする**（viewer は 403）。
            # §3.4 と同じ立て付けの判断で、根拠は3つ:
            #   1. **run ファミリの一部**。ジョブ状態は「実行した人が実行の進み方を
            #      見る」ためのもので（T-27 の完了条件「フロントがポーリングできる」）、
            #      実行できない viewer が見る場面が無い。
            #   2. **`POST /run` より広くしない。** 実行を許していない相手に、
            #      いつ何が走ったか（cron の稼働状況・失敗の理由）を見せる要件は
            #      仕様書にない。狭いほうへ倒すのが安全側。
            #   3. **成果物は別の口で見られる。** viewer が要るのは出来上がった
            #      レポート（`GET /reports/{period}` は全ロール可）で、
            #      ジョブの内部状態ではない。
            # → §3.2 の表への追記が必要（T-38）。
            Operation.GET_RUN_JOB: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_DENY, system=_ALLOW
            ),
            # ⚠️ **生成物の配信は `GET /reports/{period}` と同じ行**（全ロール可）。
            # §6.2 の「`GET /reports/{period}`（HTML/一覧）」が指しているのは
            # まさに生成物の閲覧で、一覧を全ロールへ返しながら実体を配らないのは
            # 意味を成さない。**何を配れるかは認可ではなく許可リストで絞る**
            # （`ArtifactStore.is_servable`：config.json / raw / validation /
            # narrative / scratch / _history / _runs は配信経路に載せない）。
            # → §3.2 の表への追記が必要（T-38）。
            Operation.GET_FILES: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            # === 認証（TASKS.md T-09。§6.2 には無い方針変更分）===============
            # 未認証で到達する経路。ロール別の升目は「ログイン済みでも叩ける」意。
            Operation.POST_AUTH_REGISTER: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            Operation.POST_AUTH_LOGIN: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            Operation.POST_AUTH_LOGOUT: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            # 認証済みの全ロール可。
            Operation.GET_AUTH_ME: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            Operation.POST_AUTH_PASSWORD: _row(
                admin=_ALLOW, editor=_ALLOW, viewer=_ALLOW, system=_ALLOW
            ),
            # === ユーザー管理（T-42）=========================================
            # ⚠️ `system` も 403。ユーザー管理は人が行う操作で、§6.2 の
            # `internal_only`（パイプラインの内部読込）にも当てはまらない。
            Operation.GET_USERS: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
            Operation.PATCH_USER_ROLE: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
            Operation.PATCH_USER_STATUS: _row(
                admin=_ALLOW, editor=_DENY, viewer=_DENY, system=_DENY
            ),
        }
    )
)


PUBLIC_OPERATIONS: Final[frozenset[Operation]] = frozenset(
    {
        # 未ログインの人が使うための入口。ここを閉じるとログインできない。
        Operation.POST_AUTH_REGISTER,
        Operation.POST_AUTH_LOGIN,
        # ⚠️ **ログアウトは未認証でも 204（べき等）**。TASKS.md T-40 の完了条件
        # 「`POST /auth/logout`：該当セッションを失効させ Cookie を削除。**べき等**」
        # に従った T-40 の実装で、テストでも固定されている。
        # TASKS.md T-09 の完了条件は logout を `/auth/me` と同じ「認証済みの全ロール可」
        # に分類しているが、**実装・テストとして確定しているのは T-40 のべき等要件**
        # なのでそちらを正とした。切れたセッションでログアウトを叩いた利用者に 401 を
        # 返しても、やることは同じ（Cookie を捨てる）で、増えるのは失敗表示だけ。
        Operation.POST_AUTH_LOGOUT,
    }
)


ROUTE_OPERATIONS: Final[Mapping[tuple[str, str], Operation]] = MappingProxyType(
    {
        # (HTTP メソッド, ルートのパステンプレート) → Operation。
        # パスは `APIRouter(prefix=...)` を含んだ最終形（`request.scope["route"].path`
        # と同じ表記）にすること。
        ("GET", "/config"): Operation.GET_CONFIG,
        ("PUT", "/config"): Operation.PUT_CONFIG,
        ("GET", "/config/history"): Operation.GET_CONFIG_HISTORY,
        ("POST", "/auth/register"): Operation.POST_AUTH_REGISTER,
        ("POST", "/auth/login"): Operation.POST_AUTH_LOGIN,
        ("POST", "/auth/logout"): Operation.POST_AUTH_LOGOUT,
        ("GET", "/auth/me"): Operation.GET_AUTH_ME,
        ("POST", "/auth/password"): Operation.POST_AUTH_PASSWORD,
        ("GET", "/users"): Operation.GET_USERS,
        ("PATCH", "/users/{user_id}/role"): Operation.PATCH_USER_ROLE,
        ("PATCH", "/users/{user_id}/status"): Operation.PATCH_USER_STATUS,
        # T-27。⚠️ **パスはルートのテンプレートそのまま。** `POST /run/{run_type}`
        # と `GET /run/{job_id}` は**同じ接頭辞だがメソッドが違う**ので別の行。
        ("POST", "/run/{run_type}"): Operation.POST_RUN,
        ("GET", "/run/{job_id}"): Operation.GET_RUN_JOB,
        ("GET", "/reports/{period}"): Operation.GET_REPORTS,
        ("GET", "/files/{filename}"): Operation.GET_FILES,
        # T-29。⚠️ 明細のダウンロードは**パスの末尾がファイル名リテラル**
        # （`result.xlsx`）。ルートのテンプレートをそのまま書くこと。
        ("POST", "/config/dry-run"): Operation.POST_CONFIG_DRY_RUN,
        (
            "GET",
            "/config/dry-run/{dry_run_id}/result.xlsx",
        ): Operation.GET_CONFIG_DRY_RUN_RESULT,
    }
)


def resolve_operation(method: str, route_path: str) -> Operation | None:
    """(メソッド, ルートのパステンプレート) から `Operation` を引く。

    対応が無ければ `None`。**呼び出し側は `None` を「許可」に倒さないこと**
    （マトリクスに行の無いエンドポイントは、認可されていないエンドポイント）。
    """
    return ROUTE_OPERATIONS.get((method.upper(), route_path))


def roles_allowed_over_http(operation: Operation) -> frozenset[Role]:
    """HTTP 経由で `operation` を実行できるロール。

    ⚠️ `internal_only` は**含めない**（モジュール冒頭の警告）。
    """
    return frozenset(
        role
        for role, decision in PERMISSION_MATRIX[operation].items()
        if decision is Decision.ALLOW
    )


def authorize(operation: Operation, role: Role | None) -> Outcome:
    """設計書 §4.2 の認可判定。`role=None` は未認証。

    `internal_only` は `deny` と同じく `FORBIDDEN`。理由はモジュール冒頭の警告。
    """
    if operation in PUBLIC_OPERATIONS:
        return Outcome.ALLOWED
    if role is None:
        return Outcome.UNAUTHENTICATED
    if PERMISSION_MATRIX[operation][role] is Decision.ALLOW:
        return Outcome.ALLOWED
    return Outcome.FORBIDDEN


ADMIN_ONLY_OPERATIONS: Final[frozenset[Operation]] = frozenset(
    operation
    for operation in Operation
    if operation not in PUBLIC_OPERATIONS
    and roles_allowed_over_http(operation) == frozenset({Role.ADMIN})
)
"""admin だけが HTTP で実行できるオペレーション（マトリクスから導出）。

`require_admin` を付けたルートが本当に admin 限定かの検査に使う
（`test_rbac.py::test_require_admin_is_only_used_on_admin_only_routes`）。
"""


__all__ = [
    "ADMIN_ONLY_OPERATIONS",
    "PERMISSION_MATRIX",
    "PUBLIC_OPERATIONS",
    "ROUTE_OPERATIONS",
    "Decision",
    "Operation",
    "Outcome",
    "authorize",
    "resolve_operation",
    "roles_allowed_over_http",
]
