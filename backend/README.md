# ai-intelligence-apps

社内標準の Python バックエンド（FastAPI + uv）。`python-backend-bootstrap` スキルで生成。

## 必要なもの

- [uv](https://docs.astral.sh/uv/)
- Python 3.13（`uv` が `.python-version` を見て用意する）

## セットアップ & 起動

```bash
cp .env.example .env
make sync          # 依存を同期
make migrate-all   # DB(SQLite) を作成してマイグレーション適用
make dev           # http://localhost:8000 で起動
curl http://localhost:8000/healthz   # {"status":"ok"}
curl http://localhost:8000/readyz    # {"status":"ready"}
```

**Docker は不要です。** 既定の DB は SQLite（`var/ai_intelligence.db`）で、`make up` を実行する必要はありません。

## よく使うコマンド

```bash
make lint          # ruff: 静的解析 + フォーマット検査
make format        # ruff: 自動整形 + import 整列
make type-check    # ty: 型チェック（ベストエフォート）
make test          # pytest
make test-ci       # カバレッジ付き pytest

make config-schema        # config.json の JSON Schema を生成
make config-schema-check  # 生成済みスキーマが最新かを検査
```

`make help` の後ろに `migrate-*` / `up` / `down` / `db-init` が出る（`Makefile.db.mk`）。

## 判断基準ファイル `config.json` のスキーマ

`config.json` の構造は **Pydantic モデルが正**（[`src/enterprise/entities/config.py`](src/enterprise/entities/config.py)）。
[`schemas/config.schema.json`](schemas/config.schema.json) はそこからの **生成物**（JSON Schema draft 2020-12）で、
手で編集しない。モデルを変えたら `make config-schema` で生成し直してコミットする
（ズレは `tests/enterprise/test_config_model.py` が検出する）。

カテゴリ7ID・タグ10ID・軸6ID・`scoring_total`・enum の日本語値は固定値で、
`weight` / `severity` / `enabled` / `priority` / `tunable_thresholds` が admin の編集対象
（仕様書 §5.1・§7.2）。`Σ weight == 100` などのクロスフィールド制約はモデルではなく
別モジュールが担う（設計書 §2.1.1）。

## 認証・認可

**ID（メールアドレス）／パスワード認証を自前で実装する**（[`../TASKS.md`](../TASKS.md) §1.1「認証」）。
**SSO 連携は行わない** — 2026-08-13 の方針変更で、将来の選択肢へ格下げした
（[`../docs/future-roadmap.md`](../docs/future-roadmap.md) 構想3）。

> ⚠️ `docs/spec.md` §1.3 と `docs/design.md` §3.1 には **SSO 前提の記述が残っている**。
> 改訂は T-38 で行う。それまでの実装方針の正は TASKS.md §1.1。

### パスワードの扱い

ハッシュ化は **Passlib + bcrypt** に委ねる（[`src/enterprise/services/password.py`](src/enterprise/services/password.py)）。
**ソルト生成・ストレッチ・比較を自前で書かないこと。**

- 平文は保存しない。DB に入るのは bcrypt ハッシュ（`$2b$12$…`）だけ
- ポリシーは**長さのみ**（12文字以上・UTF-8 で 72 バイト以内）。記号必須などの複雑性要件は課さない
- ⚠️ **72 バイト超は切り詰めず拒否する。** bcrypt は 73 バイト目以降を無視するため、
  切り詰めると「先頭 72 バイトが同じ別のパスワード」でログインできてしまう。
  日本語は1文字3バイトなので **24文字でこの上限に達する**（文字数ではなくバイト長で検証すること）

> ⚠️ **`bcrypt` を 5.x へ上げないこと**（`pyproject.toml` で `<5` に固定）。
> passlib 1.7.4 はバックエンド初期化時に 72 バイト超の probe を渡すが、bcrypt 5.0 は
> それを `ValueError` にするため **`hash()` が必ず失敗する**。

### ロール

`admin` / `editor` / `viewer` / `system`（[`src/enterprise/entities/principal.py`](src/enterprise/entities/principal.py)）。

- **自己登録すると必ず `viewer`。** 登録 API はロールを受け取らない
- `editor` / `admin` への昇格は **admin のみ**（T-42）。**最初の admin は CLI で作る**（T-41）
- ロールは**リクエストごとに `users` 行から解決する**（セッションに焼き込まない）。
  そのため昇格・降格が**再ログインなしで次のリクエストから効く**
- `system` は**ログインする利用者ではない**。cron 用の呼び出し元種別で、`users` に行を持てない（DB の CHECK 制約）

### 認証バックエンドの差し替え口

「リクエスト → `Principal`」の解決だけが差し替え可能な境界の内側にある。

| | |
|---|---|
| **プロトコル** | `AuthenticationBackend`（[`src/adapter/http/fastapi/auth/backend.py`](src/adapter/http/fastapi/auth/backend.py)） |
| **実装するメソッド** | `async def resolve(request) -> Principal \| None` |
| **差し替え箇所** | `get_authentication_backend()`（`src/adapter/http/fastapi/auth/dependencies.py`。T-40 で作成）の戻り値1箇所 |
| **テストでの差し替え** | `app.dependency_overrides[get_authentication_backend]` |

認可（T-09）は `Principal` しか見ないので、認証方式を変えても
§6.2 の権限マトリクスに手を入れずに済む。

> ⚠️ **差し替え口があること＝SSO 対応済み、ではない。**
> 将来 SSO を足すなら、少なくとも「ロールの正は本アプリの `users.role` か IdP のクレームか」を
> 決め直す必要がある（future-roadmap.md 構想3 の表）。
>
> ⚠️ **開発用の認証スタブ（ヘッダでロールを自称できる実装）は廃止した。復活させないこと。**
> ロールを自称できる経路があると「config は admin 以外に露出しない」（仕様書 §2・§6.1）が壊れる。

## データベース

手元は **SQLite**（Docker 不要）。本番運用時、および全文検索・ベクトル検索
（[`../docs/future-roadmap.md`](../docs/future-roadmap.md) 構想1）を実装する際に
**PostgreSQL へ移行**する想定。

| | SQLite（既定） | PostgreSQL |
|---|---|---|
| 設定 | `DB_BACKEND=sqlite` | `DB_BACKEND=postgresql` |
| 保存先 | `SQLITE_PATH`（既定 `var/ai_intelligence.db`） | `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` |
| Docker | 不要 | 必要（`make up`） |

PostgreSQL へ切り替える手順:

```bash
make up                                   # postgres を起動（Docker 必要）
DB_BACKEND=postgresql make migrate-all
DB_BACKEND=postgresql make dev
```

> **移行の道を塞がないための制約**: モデル定義は SQLAlchemy に閉じ込め、
> DB 固有機能（`JSONB`・配列型・`tsvector`・pgvector 等）を使わない。
> JSON カラムは汎用 `JSON` 型を使う。

## レイヤ構成

```
src/
├── config.py                 # 設定（pydantic-settings）
├── run_local.py              # ローカル起動
├── common/                   # 横断的な部品（logger 等）
├── enterprise/               # ドメイン（外部に依存しない）
│   ├── entities/             # エンティティ・値オブジェクト
│   └── services/             # ドメインサービス（パスワードハッシュ等）
├── application/              # ユースケース（業務手順）
└── adapter/                  # 入出力（HTTP / DB など）
    └── http/fastapi/
        ├── routers/          # FastAPI のルーター
        └── auth/             # 認証（差し替え口は上記「認証・認可」を参照）
```

依存の向きは `adapter → application → enterprise`。enterprise は何にも依存しない。
