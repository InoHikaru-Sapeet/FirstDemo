"""パスワードのハッシュ化と照合（TASKS.md §1.1「認証」／T-08）。

**ハッシュ処理は Passlib + bcrypt に委ね、自前で書かない。**
ソルト生成・コスト設定・定数時間比較・エンコードはすべて Passlib の
`CryptContext` の内側にある。このモジュールがやるのは次の3つだけ:

1. アプリで使う `CryptContext` を1つに固定する（設定の散逸を防ぐ）
2. パスワードの**長さ検証**（bcrypt の 72 バイト制限。下記⚠️）
3. 平文・ハッシュが**ログや例外メッセージに出ない**ようにする

---

⚠️ **bcrypt の 72 バイト制限は、黙って切り詰めると認証が壊れる。**

bcrypt は入力の 73 バイト目以降を無視する。passlib / bcrypt 4.x はこれを
エラーにせず**黙って切り詰める**ため、対策しないと

    「先頭 72 バイトが同じ別のパスワード」でログインできてしまう

（T-08 で実測して確認済み）。したがって上限超過は切り詰めず**拒否**する。

これは日本語パスワードで現実的に踏む。UTF-8 では日本語1文字が3バイトなので、
**24文字でちょうど 72 バイト**に達する。「文字数」で検証すると見逃すため、
**必ずバイト長で検証する**こと。

---

⚠️ **`bcrypt` パッケージを 5.x に上げてはいけない**（pyproject の上限 `<5`）。
passlib 1.7.4 はバックエンド初期化時に 72 バイト超の probe を渡すが、bcrypt 5.0
はそれを ValueError にするため `hash()` が常に失敗する。詳細は pyproject のコメント。
"""

import logging
from enum import StrEnum

from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict

# bcrypt のコストパラメータ。2^12 回のストレッチ＝手元実測で 1ハッシュ約 0.2 秒。
# 上げると総当たりに強くなるがログインが遅くなる。変更しても既存ハッシュは
# そのまま照合でき、ログイン成功時に新コストへ移行する（`verify_and_update`）。
BCRYPT_ROUNDS = 12

# パスワードポリシーは**長さのみ**（TASKS.md T-08）。記号必須などの複雑性要件は
# 課さない。短く複雑な文字列より長いパスフレーズのほうが強く、複雑性要件は
# 使い回しや紙のメモを誘発するため。
MIN_PASSWORD_LENGTH = 12

# bcrypt のアルゴリズム上の上限。**設定で変更してはいけない値**。
MAX_PASSWORD_BYTES = 72


class PasswordIssueCode(StrEnum):
    """ポリシー違反の種類。HTTP 層が 422 の `issues[].code` に載せる。

    `ConfigIssueCode`（T-05）と同じく、UI がメッセージを出し分けるための
    機械可読キー。
    """

    TOO_SHORT = "password_too_short"
    TOO_LONG_IN_BYTES = "password_too_long_in_bytes"
    EMPTY = "password_empty"


class PasswordIssue(BaseModel):
    """ポリシー違反1件。

    ⚠️ `reason` に**入力されたパスワードそのものを入れない**こと。例外は
    ログに出うるため、平文が流出する経路になる。長さのような統計量だけを書く。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PasswordIssueCode
    reason: str


class PasswordPolicyError(Exception):
    """パスワードがポリシーを満たさない。HTTP 層は 422 へ変換する。

    ⚠️ `str(e)` はログに出うるので、平文を含めてはならない。
    `PasswordIssue.reason` だけを連結する。
    """

    def __init__(self, issues: list[PasswordIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.reason for issue in issues))


def _build_context() -> CryptContext:
    """アプリ唯一の `CryptContext` を組み立てる。

    `deprecated="auto"` により、`BCRYPT_ROUNDS` を上げた後は既存ハッシュが
    「古い」と判定され、`verify_and_update` が再ハッシュを返すようになる。
    """
    context = CryptContext(
        schemes=["bcrypt"],
        deprecated="auto",
        bcrypt__rounds=BCRYPT_ROUNDS,
    )

    # passlib 1.7.4 は bcrypt のバージョンを `bcrypt.__about__` から読もうとするが、
    # 現行の bcrypt にこの属性が無いため、**初回利用時に警告とトレースバックを
    # 1度だけ吐く**（動作自体は正常。bcrypt 4.3.0 で実測）。
    # 実害はないが、本番ログでは障害に見える。
    #
    # ここで**バックエンドを先読みして**その1回を握る。抑止するのはこの
    # ウォームアップの間だけで、以降の passlib の警告は通常どおり出る。
    bcrypt_logger = logging.getLogger("passlib.handlers.bcrypt")
    previous_level = bcrypt_logger.level
    bcrypt_logger.setLevel(logging.ERROR)
    try:
        context.hash("warmup")
    finally:
        bcrypt_logger.setLevel(previous_level)

    return context


_context = _build_context()


def validate_password_policy(password: str) -> list[PasswordIssue]:
    """ポリシー違反をすべて返す（違反がなければ空リスト）。

    T-05 と同じく**早期 return せず全項目を評価**する。利用者が1度の入力で
    すべての不備を直せるようにするため。
    """
    issues: list[PasswordIssue] = []

    if not password:
        issues.append(
            PasswordIssue(
                code=PasswordIssueCode.EMPTY,
                reason="パスワードを入力してください。",
            )
        )
        return issues

    if len(password) < MIN_PASSWORD_LENGTH:
        issues.append(
            PasswordIssue(
                code=PasswordIssueCode.TOO_SHORT,
                reason=(
                    f"パスワードは{MIN_PASSWORD_LENGTH}文字以上にしてください"
                    f"（現在 {len(password)} 文字）。"
                ),
            )
        )

    # ⚠️ 文字数ではなくバイト長で見る。日本語は1文字3バイトなので24文字で上限。
    byte_length = len(password.encode("utf-8"))
    if byte_length > MAX_PASSWORD_BYTES:
        issues.append(
            PasswordIssue(
                code=PasswordIssueCode.TOO_LONG_IN_BYTES,
                reason=(
                    f"パスワードは UTF-8 で {MAX_PASSWORD_BYTES} バイト以内に"
                    f"してください（現在 {byte_length} バイト）。"
                    "日本語は1文字あたり3バイトです。"
                ),
            )
        )

    return issues


def hash_password(password: str) -> str:
    """パスワードをハッシュ化する。ポリシー違反は `PasswordPolicyError`。

    **平文を保存する経路はこの関数の外に存在しない**。戻り値は
    `$2b$12$...` 形式の文字列で、ソルトを内包する。
    """
    issues = validate_password_policy(password)
    if issues:
        raise PasswordPolicyError(issues)

    return _context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """平文とハッシュを照合する。比較は passlib（定数時間）に委ねる。

    ⚠️ ここでポリシー検証をしてはいけない。ポリシーを厳しくすると、
    既存利用者が**自分のパスワードでログインできなくなる**。検証は
    登録・変更時（`hash_password`）だけで行う。

    不正な形式のハッシュ（DB の破損・移行ミス）は例外にせず `False` を返す。
    ログインの失敗理由を呼び出し元に区別させないため。
    """
    try:
        return _context.verify(password, password_hash)
    except ValueError:
        return False


def verify_and_migrate_password(
    password: str, password_hash: str
) -> tuple[bool, str | None]:
    """照合し、必要なら新しいコストのハッシュを返す。

    戻り値は `(照合できたか, 新しいハッシュ or None)`。`BCRYPT_ROUNDS` を
    上げた後、**利用者にパスワード変更を求めずに**保存済みハッシュを移行できる。
    ログイン成功時（T-40）に呼び、新ハッシュが返ってきたら保存する。
    """
    try:
        is_valid, new_hash = _context.verify_and_update(password, password_hash)
    except ValueError:
        return False, None

    return is_valid, new_hash


def needs_rehash(password_hash: str) -> bool:
    """保存済みハッシュが現在の設定より古いか（`BCRYPT_ROUNDS` 変更後の判定用）。"""
    return _context.needs_update(password_hash)
