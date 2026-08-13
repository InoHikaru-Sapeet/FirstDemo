"""初期 admin 作成 CLI（T-41）。

重点は「破ると平文パスワードが漏れる／CLI が常用される」性質:

- パスワードを**引数・環境変数から受け取る経路が無い**
- 入力は `getpass`（エコーしない）で、確認のため2回
- 画面出力にパスワードが出ない
- 拒否されるケースではパスワードを**聞く前に**中止する
- 終了コードで「拒否」と「入力不備」を区別する
"""

import getpass
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from adapter.cli import create_admin as cli
from adapter.cli.create_admin import (
    DEFAULT_PROMPTER,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_REFUSED,
    Prompter,
    run,
)
from adapter.database.base import Base
from adapter.database.models.user import User
from enterprise.entities.principal import Role
from enterprise.services.password import hash_password, verify_password

PASSWORD = "correct horse battery staple"
EMAIL = "admin@sapeet.com"
DISPLAY_NAME = "管理 太郎"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class FakeTerminal:
    """対話入力と出力の記録。

    `secrets` に積んだ値を `read_secret` が順に返す（1回目＝入力、2回目＝確認）。
    """

    def __init__(
        self,
        lines: list[str] | None = None,
        secrets: list[str] | None = None,
    ) -> None:
        self.lines = list(lines or [])
        self.secrets = list(secrets or [])
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []
        self.output: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)

    def read_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        if not self.secrets:
            raise EOFError
        return self.secrets.pop(0)

    def write(self, message: str) -> None:
        self.output.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.output)

    def as_prompter(self) -> Prompter:
        return Prompter(read_line=self.read_line, read_secret=self.read_secret)


def terminal(secrets: list[str] | None = None, lines: list[str] | None = None):  # noqa: ANN201
    return FakeTerminal(lines=lines, secrets=secrets or [PASSWORD, PASSWORD])


async def add_user(db: AsyncSession, email: str, role: Role) -> User:
    from datetime import UTC, datetime

    now = datetime(2026, 8, 1, tzinfo=UTC)
    user = User(
        user_id=f"usr_{email}",
        email=email,
        display_name="既存 花子",
        password_hash=hash_password(PASSWORD),
        role=role,
        is_active=True,
        created_at=now,
        updated_at=now,
        password_updated_at=now,
        failed_login_attempts=0,
        locked_until=None,
    )
    db.add(user)
    await db.commit()
    return user


async def stored_users(db: AsyncSession) -> list[User]:
    return list((await db.execute(select(User))).scalars())


# --- パスワードの受け取り方（最重要）--------------------------------------


def test_there_is_no_password_option() -> None:
    """⚠️ 引数で渡せるようにしないこと（ps・シェル履歴・CI ログに残る）。"""
    parser = cli._build_parser()
    options = {option for action in parser._actions for option in action.option_strings}

    assert not [option for option in options if "password" in option]


def test_passing_a_password_flag_fails() -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["--password", PASSWORD])


def test_the_module_reads_no_environment_variables() -> None:
    """⚠️ 環境変数経路も作らない（`.env` に平文が残り続ける）。

    ソースに `environ` / `getenv` が現れないことで固定する（コメントや docstring に
    書くだけでも落ちるが、そのくらい厳しくしておいてよい約束）。
    """
    source = inspect.getsource(cli)

    assert "environ" not in source
    assert "getenv" not in source


def test_the_default_prompt_does_not_echo() -> None:
    """`getpass` は端末のエコーを止める。`input` に変えてはいけない。"""
    assert DEFAULT_PROMPTER.read_secret is getpass.getpass


async def test_an_environment_variable_cannot_supply_the_password(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境変数に置いても使われない（採用されるのは対話入力だけ）。"""
    env_password = "password from the environment"
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", env_password)
    monkeypatch.setenv("ADMIN_PASSWORD", env_password)
    fake = terminal()

    exit_code = await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_OK
    user = (await stored_users(db))[0]
    assert verify_password(PASSWORD, user.password_hash) is True
    assert verify_password(env_password, user.password_hash) is False


async def test_the_password_is_asked_twice(db: AsyncSession) -> None:
    fake = terminal()

    await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert len(fake.secret_prompts) == 2


async def test_a_mismatched_confirmation_creates_nothing(db: AsyncSession) -> None:
    fake = terminal(secrets=[PASSWORD, "a different passphrase"])

    exit_code = await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_INVALID_INPUT
    assert await stored_users(db) == []
    assert "一致しませんでした" in fake.text


async def test_the_password_never_appears_in_the_output(db: AsyncSession) -> None:
    fake = terminal()

    await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert PASSWORD not in fake.text
    assert "$2b$" not in fake.text


# --- 作成 -----------------------------------------------------------------


async def test_it_creates_an_admin(db: AsyncSession) -> None:
    fake = terminal()

    exit_code = await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_OK
    user = (await stored_users(db))[0]
    assert user.role == Role.ADMIN
    assert user.email == EMAIL
    assert verify_password(PASSWORD, user.password_hash) is True


async def test_the_email_and_name_are_asked_when_not_given(db: AsyncSession) -> None:
    """`make create-admin`（引数なし）でも完結する。"""
    fake = terminal(lines=[EMAIL, DISPLAY_NAME])

    exit_code = await run(db, prompter=fake.as_prompter(), out=fake.write)

    assert exit_code == EXIT_OK
    assert len(fake.prompts) == 2
    assert (await stored_users(db))[0].email == EMAIL


async def test_an_aborted_prompt_creates_nothing(db: AsyncSession) -> None:
    """端末が閉じた・Ctrl-D で抜けた場合（入力が尽きると EOFError）。"""
    fake = FakeTerminal()

    exit_code = await run(db, prompter=fake.as_prompter(), out=fake.write)

    assert exit_code == EXIT_INVALID_INPUT
    assert await stored_users(db) == []


async def test_a_weak_password_is_reported_with_its_reason(db: AsyncSession) -> None:
    fake = terminal(secrets=["short", "short"])

    exit_code = await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_INVALID_INPUT
    assert await stored_users(db) == []
    assert "12文字以上" in fake.text
    assert "short" not in fake.text  # 入力そのものは出さない


async def test_a_malformed_email_is_reported(db: AsyncSession) -> None:
    fake = terminal()

    exit_code = await run(
        db,
        email="not-an-email",
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_INVALID_INPUT
    assert fake.secret_prompts == []  # パスワードを聞く前に落ちる


# --- 拒否 -----------------------------------------------------------------


async def test_it_refuses_when_an_admin_already_exists(db: AsyncSession) -> None:
    await add_user(db, "first@sapeet.com", role=Role.ADMIN)
    fake = terminal()

    exit_code = await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_REFUSED
    assert len(await stored_users(db)) == 1
    assert "--promote" in fake.text


async def test_it_does_not_ask_for_a_password_it_will_not_use(
    db: AsyncSession,
) -> None:
    """⚠️ 拒否が確定しているのに長いパスワードを2回入力させない。"""
    await add_user(db, "first@sapeet.com", role=Role.ADMIN)
    fake = terminal()

    await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert fake.secret_prompts == []


async def test_an_existing_email_is_refused_with_a_hint(db: AsyncSession) -> None:
    await add_user(db, EMAIL, role=Role.VIEWER)
    fake = terminal()

    exit_code = await run(
        db,
        email=EMAIL,
        display_name=DISPLAY_NAME,
        prompter=fake.as_prompter(),
        out=fake.write,
    )

    assert exit_code == EXIT_REFUSED
    assert "--promote" in fake.text
    assert (await stored_users(db))[0].role == Role.VIEWER


# --- 昇格 -----------------------------------------------------------------


async def test_promote_upgrades_an_existing_user(db: AsyncSession) -> None:
    await add_user(db, EMAIL, role=Role.VIEWER)
    fake = terminal()

    exit_code = await run(
        db, promote=EMAIL, prompter=fake.as_prompter(), out=fake.write
    )

    assert exit_code == EXIT_OK
    assert (await stored_users(db))[0].role == Role.ADMIN
    # 昇格ではパスワードを聞かない。
    assert fake.secret_prompts == []


async def test_promote_is_idempotent(db: AsyncSession) -> None:
    await add_user(db, EMAIL, role=Role.ADMIN)
    fake = terminal()

    exit_code = await run(
        db, promote=EMAIL, prompter=fake.as_prompter(), out=fake.write
    )

    assert exit_code == EXIT_OK
    assert "既に admin" in fake.text


async def test_promoting_an_unknown_user_is_refused(db: AsyncSession) -> None:
    fake = terminal()

    exit_code = await run(
        db, promote="nobody@sapeet.com", prompter=fake.as_prompter(), out=fake.write
    )

    assert exit_code == EXIT_REFUSED
    assert await stored_users(db) == []


async def test_promoting_a_malformed_email_is_an_input_error(db: AsyncSession) -> None:
    fake = terminal()

    exit_code = await run(
        db, promote="not-an-email", prompter=fake.as_prompter(), out=fake.write
    )

    assert exit_code == EXIT_INVALID_INPUT


# --- 引数 -----------------------------------------------------------------


def test_the_parser_accepts_the_documented_options() -> None:
    args = cli._build_parser().parse_args(
        ["--email", EMAIL, "--display-name", DISPLAY_NAME, "--promote", EMAIL]
    )

    assert args.email == EMAIL
    assert args.display_name == DISPLAY_NAME
    assert args.promote == EMAIL


def test_no_arguments_are_required() -> None:
    """`make create-admin` を引数なしで叩ける（すべて対話で聞く）。"""
    args = cli._build_parser().parse_args([])

    assert args.email is None
    assert args.display_name is None
    assert args.promote is None
