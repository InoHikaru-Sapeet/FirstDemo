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
make create-admin  # 最初の admin を作る（対話。パスワードは表示されない）
make dev           # http://localhost:8000 で起動
curl http://localhost:8000/healthz   # {"status":"ok"}
curl http://localhost:8000/readyz    # {"status":"ready"}
```

**Docker は不要です。** 既定の DB は SQLite（`var/ai_intelligence.db`）で、`make up` を実行する必要はありません。

`make create-admin` は**初回だけ**必要です（詳細は下の「最初の admin を作る」）。
手元で http を使う場合は `.env` に `SESSION_COOKIE_SECURE=false` を入れてください
（既定は true ＝ HTTPS 前提のため、http ではログイン Cookie が保持されません）。

ログインの確認:

```bash
curl -c cookie.txt -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@sapeet.com","password":"..."}'
curl -b cookie.txt http://localhost:8000/auth/me   # role が admin になっている
```

## よく使うコマンド

```bash
make lint          # ruff: 静的解析 + フォーマット検査
make format        # ruff: 自動整形 + import 整列
make type-check    # ty: 型チェック（ベストエフォート）
make test          # pytest
make test-ci       # カバレッジ付き pytest

make config-schema        # config.json の JSON Schema を生成
make config-schema-check  # 生成済みスキーマが最新かを検査

make create-admin         # 最初の admin を作る（T-41）
make service-token        # cron 用サービストークンを発行（T-41）
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
- `editor` / `admin` への昇格は **admin のみ**（T-42）。**最初の admin は CLI で作る**
  （`make create-admin`。下の「最初の admin を作る」）
- ロールは**リクエストごとに `users` 行から解決する**（セッションに焼き込まない）。
  そのため昇格・降格が**再ログインなしで次のリクエストから効く**
- `system` は**ログインする利用者ではない**。cron 用の呼び出し元種別で、`users` に行を持てない
  （DB の CHECK 制約）。経路はサービストークンのみ（下の「cron 用サービストークン」）

### 最初の admin を作る（`make create-admin`）

自己登録は**必ず `viewer`**、昇格できるのは **admin だけ**。つまり**最初の1人は
API では作れない**（昇格させる admin がまだ居ない）。そこで DB へ直接書ける経路を
**この CLI 1本に限って**正式化している（[`src/adapter/cli/create_admin.py`](src/adapter/cli/create_admin.py)）。

```bash
make create-admin                                 # メール・表示名・パスワードを対話で入力
make create-admin ARGS="--email you@sapeet.com"   # 一部だけ引数で渡す
make create-admin ARGS="--promote you@sapeet.com" # 既存ユーザーを admin へ昇格
```

> ⚠️ **パスワードは対話プロンプト（`getpass`）だけで受け取る。**
> `--password` のような引数も、環境変数も**用意していない**（足さないこと）。
> 引数は `ps` で他ユーザーに見え、シェル履歴と CI ログに残る。環境変数は `.env` に
> 平文が残り続ける。確認のため2回入力し、不一致なら何も書かずに終了する。

| 状況 | 挙動 | 終了コード |
|---|---|---|
| admin が居ない・メール未登録 | 作成する | 0 |
| **admin が既に居る** | **拒否**し、`--promote` を案内する（CLI を常用させない） | 1 |
| 同じメールが既に登録済み | **何もしない**。`--promote` を案内する（黙って書き換えない） | 1 |
| `--promote` で対象が viewer/editor | admin へ昇格（パスワードは変更しない） | 0 |
| `--promote` で対象が既に admin | 何もしない（**べき等**） | 0 |
| `--promote` で対象が存在しない | 拒否 | 1 |
| メール形式・パスワードポリシー違反・確認不一致 | 拒否（何も書かない） | 2 |

- 停止中（`is_active=false`）の admin も「居る」と数える。停止するだけで2人目を作れては困るため
- ロール変更は監査ログに残る（`user_role_change` / actor `cli:create-admin`）
- 通常の昇格・降格は admin としてログインして `PATCH /users/{user_id}/role`（T-42）。
  `--promote` は**ブートストラップと復旧**のための手段

### cron 用サービストークン（`make service-token`）

`system` は**ログインする利用者ではなく呼び出し元の種別**なので Cookie を持てない。
cron は `Authorization: Bearer <token>` を提示する。

```bash
make service-token     # 生トークンと、.env に入れるハッシュを表示する
```

```bash
# cron 側
curl -X POST http://localhost:8000/run/weekly -H "Authorization: Bearer <生トークン>"
```

> ⚠️ **`.env` に入れるのはハッシュ（`SERVICE_TOKEN_HASH`）で、生トークンではない。**
> アプリが保存するのは SHA-256 ハッシュだけなので、**設定ファイルが漏れても
> そのままでは system を騙れない**（生トークンはハッシュから復元できない）。
> 生トークンは cron 側の秘密情報として渡す（systemd の `EnvironmentFile` 等）。
>
> ⚠️ **生トークンは発行時の1回しか表示されない。** 失くしたら再発行する。
>
> ⚠️ **`SERVICE_TOKEN_HASH` が未設定なら system 経路そのものが無効。**
> 「未設定なら誰でも system」にはならない（そうなると §6.2 の認可が崩れる）。

照合は `secrets.compare_digest`（定数時間）で行う。`==` に変えると、応答時間の差から
トークンを1バイトずつ復元できる。

### 認証バックエンドの差し替え口

「リクエスト → `Principal`」の解決だけが差し替え可能な境界の内側にある。

| | |
|---|---|
| **プロトコル** | `AuthenticationBackend`（[`src/adapter/http/fastapi/auth/backend.py`](src/adapter/http/fastapi/auth/backend.py)） |
| **実装するメソッド** | `async def resolve(request) -> Principal \| None` |
| **差し替え箇所** | `get_authentication_backend()`（`src/adapter/http/fastapi/auth/dependencies.py`。T-40 で作成）の戻り値1箇所 |
| **テストでの差し替え** | `app.dependency_overrides[get_authentication_backend]` |

現在の実装は2方式の合成（[`chain.py`](src/adapter/http/fastapi/auth/chain.py)）で、**順に試して最初に解決したもので確定する**。

| 順 | 方式 | 対象 | 無効化の条件 |
|---|---|---|---|
| 1 | サービストークン（`Authorization: Bearer`） | cron（`system`） | `SERVICE_TOKEN_HASH` 未設定 |
| 2 | Cookie セッション（`sid`） | 人（admin / editor / viewer） | — |

> ⚠️ **この順序を入れ替えないこと。** Cookie を先にすると、Bearer を提示した
> リクエストが Cookie の人のロールで通りうる。

認可（T-09）は `Principal` しか見ないので、認証方式を変えても
§6.2 の権限マトリクスに手を入れずに済む。

> ⚠️ **差し替え口があること＝SSO 対応済み、ではない。**
> 将来 SSO を足すなら、少なくとも「ロールの正は本アプリの `users.role` か IdP のクレームか」を
> 決め直す必要がある（future-roadmap.md 構想3 の表）。
>
> ⚠️ **開発用の認証スタブ（ヘッダでロールを自称できる実装）は廃止した。復活させないこと。**
> ロールを自称できる経路があると「config は admin 以外に露出しない」（仕様書 §2・§6.1）が壊れる。

## AI 呼び出し（Claude Code CLI）

パイプライン（crawl / filter）の AI 呼び出しは **Claude Code CLI（`claude -p`）を
サブプロセスとして実行**する。認証は**会社の Team 契約でログイン済みの CLI セッション**で、
**APIキーは使わない**（TASKS.md §1.1「AI呼び出し方式」／T-15）。

> ⚠️ **実行前提**: `claude` が PATH にあり、**ログイン済み**であること。
> 満たされない場合は握り潰さず例外で落ちる（下表）。
>
> ⚠️ **これは試作段階の手段。** 本番（AWS 展開・無人での定期実行）では
> Anthropic API 実装を追加して差し替える。そのため `anthropic` 依存と
> `ANTHROPIC_API_KEY` は**残してある**（現行の CLI 実装では使わない）。

### 差し替え口

| | |
|---|---|
| **プロトコル** | `AIClient`（[`src/adapter/llm/ai_client.py`](src/adapter/llm/ai_client.py)） |
| **実装するメソッド** | `async def complete(*, prompt, output_schema, prompt_version, timeout) -> AIResult[T]` |
| **差し替え箇所** | `get_ai_client()`（[`src/adapter/llm/__init__.py`](src/adapter/llm/__init__.py)）の戻り値1箇所 |
| **テストでの差し替え** | `CommandRunner`（サブプロセスの実行だけを差し替え。実際の `claude` は起動しない） |

上位（crawl / filter）が渡すのは**プロンプトと出力スキーマだけ**で、呼び出し先が
CLI か API かを知らない。構造化出力は「JSON Schema を添えて JSON のみを出力させる
＋ `result` を Pydantic で検証 ＋ 不一致ならパースエラーを載せて再依頼」で担保する。

### 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `AI_CLI_COMMAND` | `claude` | 実行するコマンド（絶対パスも可） |
| `ANTHROPIC_MODEL` | `claude-opus-5` | `--model` に渡す値 |
| `AI_TIMEOUT_SECONDS` | `600`（10分） | 分類・採点系の既定 |
| `AI_CRAWL_TIMEOUT_SECONDS` | `1800`（30分） | crawl は呼び出し側がこれを渡す |
| `AI_MAX_ATTEMPTS` | `3` | **スキーマ不一致時**の試行回数上限 |
| `AI_RETRY_BACKOFF_SECONDS` | `2.0` | リトライ前の待ち時間（指数で伸びる） |

> ⚠️ **タイムアウトの既定を短くしないこと。** `1+1` のような些細なプロンプトでも
> **約131秒**かかることを実測している（CLI の起動・初期化のオーバーヘッド）。
> 短い既定は「本番相当の実行が途中で殺される」形で現れる。

### 失敗の種類

`AIClientError` を基底に、原因ごとに別の例外で返る（呼び出し元が再実行の可否を
判断できるようにするため。標準エラー出力の内容を必ずメッセージへ載せる）。

| 例外 | いつ |
|---|---|
| `AIUnavailableError` | `claude` が PATH に無い／実行権限が無い（**再実行では直らない**） |
| `AITimeoutError` | 制限時間を超えた（プロセスは kill 済み） |
| `AIProcessError` | 終了コードが非0（**未ログインはここに出る想定** — 未実測） |
| `AIProtocolError` | 標準出力が `--output-format json` の封筒として読めない |
| `AIResponseError` | 封筒が失敗を申告（`is_error` / `subtype` / `api_error_status` / `stop_reason=refusal`） |
| `AIOutputParseError` | 出力が期待スキーマに合わない（上限まで再試行した後） |

> ⚠️ **成功判定を終了コード0だけに頼らない。** 実測では成功時に 0 が返るが、
> **失敗時の終了コード・出力形式は未実測**。封筒の申告と終了コードが矛盾したら
> 失敗側（安全側）へ倒す。**リトライするのはスキーマ不一致だけ**で、
> プロセス失敗・タイムアウトはジョブ単位の再実行に委ねる。

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
└── adapter/                  # 入出力（HTTP / DB / CLI など）
    ├── cli/                  # 運用コマンド（create-admin / service-token 等）
    ├── llm/                  # AI 呼び出し（差し替え口は上記「AI 呼び出し」を参照）
    └── http/fastapi/
        ├── routers/          # FastAPI のルーター
        └── auth/             # 認証（差し替え口は上記「認証・認可」を参照）
```

依存の向きは `adapter → application → enterprise`。enterprise は何にも依存しない。
