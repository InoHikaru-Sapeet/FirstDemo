"""cron 用サービストークンを発行する CLI（TASKS.md T-41）。

    make service-token

`system` ロールは**ログインする利用者ではなく呼び出し元の種別**なので、cron は
Cookie を持てない。代わりに `Authorization: Bearer <token>` を提示する
（受け取り側は `adapter/http/fastapi/auth/service_token.py`）。

---

⚠️ **生トークンはこの1回しか表示されない。** アプリ側が保存するのは SHA-256
ハッシュだけで、ハッシュから生トークンは復元できない（DB も設定ファイルも
漏れたところで system を騙れないようにするための設計）。失くしたら再発行する。

⚠️ **`.env` に置くのはハッシュ（`SERVICE_TOKEN_HASH`）。** 生トークンを置かない。
生トークンは cron 側の秘密情報として渡す（systemd の `EnvironmentFile`・
秘密管理サービス等）。

⚠️ **端末のスクロールバック・画面共有に残る点は避けられない。** 控えたら
`clear` するか、リダイレクト先のファイルを消すこと。
"""

from argparse import ArgumentParser

from enterprise.services.service_token import (
    generate_service_token,
    hash_service_token,
)


def format_instructions(raw_token: str, token_hash: str) -> str:
    """発行結果と設定手順を組み立てる（テストしやすいよう文字列で返す）。"""
    return "\n".join(
        [
            "サービストークンを発行しました（cron / 非対話クライアント用）。",
            "",
            "1) アプリ側の設定（.env）に「ハッシュ」を追記する:",
            "",
            f"   SERVICE_TOKEN_HASH={token_hash}",
            "",
            "2) cron 側にだけ「生トークン」を渡す（⚠️ 再表示できません）:",
            "",
            f"   {raw_token}",
            "",
            "   使い方:",
            "   curl -X POST http://localhost:8000/run/weekly \\",
            f'     -H "Authorization: Bearer {raw_token}"',
            "",
            "⚠️ 生トークンを .env に書かないこと（設定ファイルが漏れたときに",
            "   そのまま system として使われます）。SERVICE_TOKEN_HASH を消せば",
            "   system 経路そのものが無効になります。",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    ArgumentParser(prog="service-token", description=__doc__).parse_args(argv)

    raw_token = generate_service_token()
    print(format_instructions(raw_token, hash_service_token(raw_token)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
