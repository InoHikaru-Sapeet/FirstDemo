"""最初の admin を作る CLI（TASKS.md T-41）。

    make create-admin                                   # 対話で作成
    make create-admin ARGS="--email a@sapeet.com"       # 一部を引数で渡す
    make create-admin ARGS="--promote a@sapeet.com"     # 既存ユーザーを admin へ

自己登録は常に `viewer` を作り、昇格できるのは admin だけ（T-42）。したがって
**最初の1人は API 経由では作れない**ため、DB へ直接書ける経路をこの CLI 1本に
限って正式化する。業務規則は `application/usecases/bootstrap_admin.py` にあり、
ここは入出力（引数・プロンプト・終了コード）だけを担う。

---

⚠️ **パスワードを引数・環境変数で受け取る経路を足さないこと。**

- コマンドライン引数 → `ps` で他ユーザーに見え、シェル履歴（`~/.zsh_history`）と
  CI のコマンドログに残る
- 環境変数 → `.env` に平文が残り続け、同じホストの他プロセスからプロセスの環境を
  読み出せる

受け取り方は**対話プロンプト（`getpass`）だけ**。`getpass` は端末のエコーを止める
ので、入力中の画面にも履歴にも残らない。確認のため2回入力させ、不一致なら
何も書かずに終了する。
"""

import asyncio
import getpass
from argparse import ArgumentParser
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from adapter.database.database import db_manager
from application.usecases.bootstrap_admin import (
    BootstrapAdminError,
    BootstrapAdminUsecase,
    BootstrapErrorCode,
    BootstrapOutcome,
)
from enterprise.services.password import MIN_PASSWORD_LENGTH, PasswordPolicyError

EXIT_OK = 0
# 業務規則による拒否（admin が既に居る / 対象が居ない など）。DB は変更していない。
EXIT_REFUSED = 1
# 入力の不備（メール形式・パスワードポリシー・確認不一致・中断）。
EXIT_INVALID_INPUT = 2

# 拒否のうち「入力の不備」として扱うもの。それ以外は業務規則による拒否。
_INVALID_INPUT_CODES = frozenset(
    {
        BootstrapErrorCode.EMAIL_INVALID,
        BootstrapErrorCode.DISPLAY_NAME_REQUIRED,
    }
)


class PromptAborted(Exception):
    """利用者が入力を中断した / 確認入力が一致しなかった。"""


@dataclass(frozen=True)
class Prompter:
    """対話入力の差し替え口（テストはここを置き換える）。

    ⚠️ `read_secret` の既定を `input` などエコーする関数に変えないこと。
    画面に残ったパスワードは、共有端末・画面共有・スクリーンショットで漏れる。
    """

    read_line: Callable[[str], str] = field(default=input)
    read_secret: Callable[[str], str] = field(default=getpass.getpass)


DEFAULT_PROMPTER = Prompter()


def _require_line(prompter: Prompter, prompt: str, value: str | None) -> str:
    """引数で渡されていなければ対話で聞く。"""
    if value is not None:
        return value
    try:
        return prompter.read_line(prompt)
    except EOFError as exc:  # 端末が閉じた / 入力が尽きた
        raise PromptAborted("入力が中断されました。") from exc


def read_new_password(prompter: Prompter) -> str:
    """パスワードを2回聞いて一致を確かめる。**エコーしない。**

    Raises:
        PromptAborted: 入力が中断された / 2回の入力が一致しなかった
    """
    try:
        password = prompter.read_secret(
            f"パスワード（{MIN_PASSWORD_LENGTH}文字以上・入力は表示されません）: "
        )
        confirmation = prompter.read_secret("パスワード（確認のためもう一度）: ")
    except EOFError as exc:
        raise PromptAborted("入力が中断されました。") from exc

    if password != confirmation:
        # ⚠️ どこが違うかを出さない（入力内容の断片を画面に出さないため）。
        raise PromptAborted("パスワードが一致しませんでした。何も変更していません。")

    return password


async def run(
    db: AsyncSession,
    *,
    email: str | None = None,
    display_name: str | None = None,
    promote: str | None = None,
    prompter: Prompter = DEFAULT_PROMPTER,
    out: Callable[[str], None] = print,
) -> int:
    """CLI の本体。終了コードを返す（例外で落ちない）。

    Args:
        db: DB セッション
        email: 作成するユーザーのメール（None なら対話で聞く）
        display_name: 表示名（None なら対話で聞く）
        promote: 指定するとそのメールの既存ユーザーを admin へ昇格させる
        prompter: 対話入力（テストで差し替える）
        out: 出力先（テストで差し替える）

    Returns:
        `EXIT_OK` / `EXIT_REFUSED` / `EXIT_INVALID_INPUT`
    """
    usecase = BootstrapAdminUsecase(db)

    try:
        if promote is not None:
            return await _promote(usecase, promote, out)
        return await _create(usecase, email, display_name, prompter, out)
    except BootstrapAdminError as exc:
        out(f"中止しました: {exc.message}")
        return EXIT_INVALID_INPUT if exc.code in _INVALID_INPUT_CODES else EXIT_REFUSED
    except PasswordPolicyError as exc:
        # ⚠️ 例外の文言に平文は入らない（`PasswordIssue.reason` は統計量だけ。T-08）。
        out("中止しました: パスワードがポリシーを満たしません。")
        for issue in exc.issues:
            out(f"  - {issue.reason}")
        return EXIT_INVALID_INPUT
    except PromptAborted as exc:
        out(f"中止しました: {exc}")
        return EXIT_INVALID_INPUT


async def _create(
    usecase: BootstrapAdminUsecase,
    email: str | None,
    display_name: str | None,
    prompter: Prompter,
    out: Callable[[str], None],
) -> int:
    resolved_email = _require_line(prompter, "メールアドレス: ", email)
    resolved_name = _require_line(prompter, "表示名: ", display_name)

    # ⚠️ パスワードを聞く**前に**拒否条件を確かめる。拒否されるとわかっている
    # 操作のために、長いパスワードを2回入力させない。
    await usecase.ensure_can_create_initial_admin(resolved_email, resolved_name)

    password = read_new_password(prompter)

    user = await usecase.create_initial_admin(
        email=resolved_email, display_name=resolved_name, password=password
    )

    out(f"admin を作成しました: {user.email}（user_id={user.user_id}）")
    out("次の手順: make dev で起動し、POST /auth/login でログインしてください。")
    return EXIT_OK


async def _promote(
    usecase: BootstrapAdminUsecase, email: str, out: Callable[[str], None]
) -> int:
    outcome, user = await usecase.promote_to_admin(email)

    if outcome is BootstrapOutcome.ALREADY_ADMIN:
        # ⚠️ 何もしなかったことを 0 で返す（再実行しても壊れない＝べき等）。
        out(f"{user.email} は既に admin です。変更していません。")
        return EXIT_OK

    out(f"admin へ昇格させました: {user.email}（user_id={user.user_id}）")
    return EXIT_OK


def _build_parser() -> ArgumentParser:
    """引数パーサ。

    ⚠️ **`--password` を足さないこと**（モジュール冒頭の理由）。パスワードは
    対話プロンプトでしか受け取らない。
    """
    parser = ArgumentParser(
        prog="create-admin",
        description=(
            "最初の admin を作成する（T-41）。"
            "パスワードは対話プロンプトでのみ受け取り、引数・環境変数では渡せない。"
        ),
    )
    parser.add_argument("--email", help="作成するユーザーのメールアドレス")
    parser.add_argument("--display-name", help="表示名")
    parser.add_argument(
        "--promote",
        metavar="EMAIL",
        help=(
            "既存ユーザーを admin へ昇格させる（admin が既に居る場合の手段。"
            "パスワードは変更しない）"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    async def _run() -> int:
        async with db_manager.session() as db:
            return await run(
                db,
                email=args.email,
                display_name=args.display_name,
                promote=args.promote,
            )

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("中止しました: 中断されました。")
        return EXIT_INVALID_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
