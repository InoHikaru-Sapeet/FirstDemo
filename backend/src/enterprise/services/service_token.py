"""cron 等の非対話クライアント用サービストークン（TASKS.md §1.1／T-41）。

`system` ロールは**ログインする利用者ではなく、呼び出し元の種別**（TASKS.md T-08）。
cron は Cookie を持てないので、`Authorization: Bearer <token>` を別経路として用意し、
このモジュールがその発行・検証を担う。

---

**設計上、動かしてはいけない点**

1. **平文のトークンをどこにも保存しない。** 保存するのは SHA-256 ハッシュだけで、
   生トークンは発行の瞬間にしか存在しない（セッショントークンと同じ扱い。T-40）。
   `.env` に置くのも**ハッシュ**（`SERVICE_TOKEN_HASH`）で、生トークンは
   cron 側の秘密情報として渡す。設定ファイルが漏れてもそのままでは system を騙れない。
2. **比較は `secrets.compare_digest`（定数時間）で行う。** `==` は先頭から一致した
   分だけ時間がかかるため、応答時間の差からトークンを1バイトずつ当てられる。
3. **bcrypt は使わない。** 生トークンは 256 ビットの乱数で辞書攻撃の対象にならず、
   検証は cron の全リクエストで走る。理由は `hash_session_token()`（T-40）と同じ。
4. **未設定なら system 経路そのものを無効化する。** 空のハッシュを「何にでも一致」
   としてはならない（`verify_service_token` が常に False を返す）。
"""

import hashlib
import secrets

# 生トークンのバイト数。`secrets.token_urlsafe(32)` は 256 ビットの乱数から
# 43文字の URL-safe 文字列を作る（セッショントークンと同じ強度。T-40）。
SERVICE_TOKEN_BYTES = 32

# SHA-256 を16進で表した長さ。`SERVICE_TOKEN_HASH` はこの固定長になる。
SERVICE_TOKEN_HASH_LENGTH = 64

# サービストークンで解決される `Principal.subject`。監査ログの actor は
# `system:cron` になる（設計書 §4.4 の `role:subject` 形式）。
SERVICE_TOKEN_SUBJECT = "cron"


def generate_service_token() -> str:
    """新しい生トークンを発行する。

    ⚠️ 戻り値は**保存せずに**呼び出し元へ渡すだけにすること。DB・設定ファイル・
    ログへ書いてよいのは `hash_service_token()` の結果だけ。
    """
    return secrets.token_urlsafe(SERVICE_TOKEN_BYTES)


def hash_service_token(raw_token: str) -> str:
    """生トークンから、設定に保存する照合用ハッシュ（SHA-256 の16進）を作る。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def looks_like_service_token_hash(value: str) -> bool:
    """設定値がハッシュの形をしているか（64桁の16進）。

    ⚠️ これは**暗号的な検査ではなく、運用ミスの検出**。最も起きやすい事故は
    `SERVICE_TOKEN_HASH` に**生トークンをそのまま貼ってしまう**こと。その場合
    照合は必ず失敗する（安全側に倒れる）が、原因がわからないまま
    「cron が 401 になる」で止まるため、呼び出し側が警告を出せるようにしておく。
    """
    normalized = _normalize_hash(value)
    if len(normalized) != SERVICE_TOKEN_HASH_LENGTH:
        return False
    return all(c in "0123456789abcdef" for c in normalized)


def verify_service_token(presented_token: str, expected_hash: str) -> bool:
    """提示されたトークンが設定のハッシュに一致するか。

    Args:
        presented_token: `Authorization: Bearer` で提示された生トークン
        expected_hash: 設定（`SERVICE_TOKEN_HASH`）に入っているハッシュ

    Returns:
        一致すれば True。**どちらかが空なら常に False**（未設定＝system 経路は無効）。
    """
    if not presented_token or not expected_hash:
        return False

    return secrets.compare_digest(
        hash_service_token(presented_token), _normalize_hash(expected_hash)
    )


def _normalize_hash(value: str) -> str:
    """設定値の表記ゆれ（前後の空白・大文字16進）を吸収する。

    `.env` からの読み込みでは空白が混ざりやすく、16進は大文字でも同じ値のため。
    ⚠️ 吸収するのはここまで。値そのものを補完・切り詰めはしない。
    """
    return value.strip().lower()
