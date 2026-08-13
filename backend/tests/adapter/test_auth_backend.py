"""認証バックエンドの差し替え口（T-08）。

「リクエスト → Principal」の解決だけがこのプロトコルの内側にあり、
認可（T-09）は `Principal` しか見ない。この境界を固定しておくことで、
将来 SSO を足すときに認可へ手を入れずに済む（TASKS.md §1.1「認可」）。
"""

import inspect

from fastapi import Request

from adapter.http.fastapi.auth import backend as backend_module
from adapter.http.fastapi.auth.backend import AuthenticationBackend
from enterprise.entities.principal import Principal, Role


class _StubBackend:
    """プロトコルを満たす最小実装（テスト内でのみ使う）。"""

    async def resolve(self, request: Request) -> Principal | None:
        return Principal(subject="u1", role=Role.ADMIN)


def test_an_implementation_satisfies_the_protocol() -> None:
    assert isinstance(_StubBackend(), AuthenticationBackend)


def test_an_object_without_resolve_does_not_satisfy_the_protocol() -> None:
    class Incomplete:
        pass

    assert not isinstance(Incomplete(), AuthenticationBackend)


def test_resolve_returns_an_optional_principal() -> None:
    """未認証は**例外ではなく `None`**。

    未認証（401）と権限なし（403）は HTTP 層で区別して返す必要があり（T-09）、
    例外で「未認証」を表すとその区別が呼び出し側から見えなくなる。
    """
    signature = inspect.signature(AuthenticationBackend.resolve)

    assert signature.return_annotation == Principal | None


def test_the_protocol_does_not_expose_authorization_helpers() -> None:
    """認可をここに持ち込まない（判定が2箇所に散ると §6.2 との1:1 が崩れる）。"""
    members = {name for name in vars(AuthenticationBackend) if not name.startswith("_")}

    assert members == {"resolve"}


def test_the_development_stub_backend_is_gone() -> None:
    """⚠️ 2026-08-13 の方針変更で開発用スタブは**廃止**した。

    ロールをヘッダから自称できる実装が残っていると、「config は admin 以外に
    露出しない」（仕様書 §2・§6.1）が壊れる。復活させないこと。
    """
    from pathlib import Path

    auth_dir = Path(backend_module.__file__).parent

    assert not (auth_dir / "stub.py").exists()
