# AI動向把握アプリケーション 仕様書
## 「週刊メルマガ（Weekly AI Intelligence）」／「月刊ビリーフ（Monthly Belief）」

> **このドキュメントの位置づけ**
> 本書は *仕様書（What/Why）* です。これを入力として、別のClaudeチャットに *設計書（How：クラス構成・DBスキーマ・API定義・シーケンス・画面遷移）* を作成させることを目的としています。
> 設計者（次のClaude）は、本書の「§13 設計書に落とし込む際の指示」を必ず先に読んでください。
> 記載のサンプル値・enum・スコア配点は、アップロードされた既存ファイル（`weekly_ai_intelligence_requirements.xlsx` ほか）から抽出した実データに基づく確定値です。推測で変更しないこと。

---

## 目次
1. 目的とスコープ
2. 登場人物（アクター）と権限
3. システム全体像とデータフロー
4. 用語定義
5. 判断基準ファイル `config.json` 仕様（★中核）
6. 権限管理仕様
7. 管理画面仕様（2アプリ共通のパラメータ編集）
8. 中間レポートファイル仕様
9. 週刊メルマガ HTML 仕様
10. 月刊ビリーフ HTML 仕様
11. 重複チェック仕様
12. スコアリング根拠フォーマットチェック仕様
13. スケジュールタスク／設定テンプレート（3プロンプト）
14. 非機能要件・エラー処理
15. 設計書に落とし込む際の指示（次のClaudeへ）

---

## 1. 目的とスコープ

### 1.1 背景・目的
社内の非専門メンバーが、AIの最新動向を「手間なく」「信頼できる形で」把握できるようにする。手動での記事収集・取捨選択・要約を廃し、**AIによるWebクローリング → 判断基準（config.json）に基づく自動フィルタリング → 定型HTMLの自動生成** までを一気通貫で自動化する。

### 1.2 2つのアプリケーション
| | 週刊メルマガ | 月刊ビリーフ |
|---|---|---|
| 正式名 | Weekly AI Intelligence by Sapeet | 月刊ビリーフ by Sapeet |
| 読み方 | 一週間の各業界のAI動向を **一目で** 把握 | 一ヶ月の先進AI活用事例を **腰を据えて** 読む |
| 単位 | 週次（ISO週番号 例: `2026-W31`） | 月次（例: `2026-07`） |
| 出力 | 業界別のニュースレターHTML（1メール） | 章立て特集HTML（15事例前後・5章前後） |
| 体裁の正 | `weekly_ai_intelligence_newsletter_不動産_2026-W31.html` | `monthly_belief_2026-07.html` |
| 中間ファイル | `weekly_ai_intelligence_report.xlsx` | `monthly_ai_leading_cases.xlsx` |

### 1.3 スコープ
- **含む**：クローリング指示（プロンプト）／フィルタリング（config.jsonによる分類・採点・除外）／中間xlsx生成／HTML生成／重複チェック／スコアリング根拠検証／権限管理／管理画面でのパラメータ編集／スケジュール実行テンプレート。
- **含まない（本フェーズ外・設計書で拡張余地として明記）**：配信基盤（メール送信・SMTP）、認証基盤の新規構築（既存SSO前提でよい）、記事本文の全文DB化。

---

## 2. 登場人物（アクター）と権限

| アクター | 説明 | config.json 表示 | config.json 編集 | 生成HTML閲覧 | 実行トリガ |
|---|---|:---:|:---:|:---:|:---:|
| **管理者 (admin)** | 判断基準を管理する運用担当 | ○ | ○ | ○ | ○ |
| **編集者 (editor)** | 生成結果を確認・軽微修正する担当 | ×（要約表示のみ可） | × | ○ | ○ |
| **閲覧者 (viewer)** | 一般社員。完成物を読むだけ | × | × | ○ | × |
| **スケジューラ (system)** | 定期実行のシステムアカウント | 読込のみ（内部） | × | ○ | ○ |

> **重要要件（顧客指定）**：`config.json`（判断基準ファイル）の **表示・編集は admin のみ**。editor / viewer には config の存在・中身を露出しない。管理画面の「パラメータ編集」タブ自体を admin 以外には非表示とする。

---

## 3. システム全体像とデータフロー

```
┌──────────────┐   ┌───────────────────────────┐   ┌────────────────────┐   ┌──────────────┐
│ 1. Crawl     │→ │ 2. Filter (config.json)   │→ │ 3. Render          │→ │ 4. Output    │
│ AIがWeb収集   │   │ 分類・10タグ付与・6軸採点  │   │ 中間xlsx→HTML化    │   │ メルマガ/     │
│ (Prompt-1)   │   │ 13除外ルール・重複除外     │   │ (Prompt-3)         │   │ ビリーフHTML  │
│              │   │ (Prompt-2)                │   │                    │   │              │
└──────────────┘   └───────────────────────────┘   └────────────────────┘   └──────────────┘
      │                      │  ▲                            │
      ▼                      ▼  │ 読込のみ                   ▼
 raw_articles.json     weekly_report.xlsx /          weekly_..._newsletter.html /
 (収集生データ)          monthly_cases.xlsx            monthly_belief.html
                       + 除外ログ(sheet)
                              ▲
                              │ 判断基準の唯一の正
                     ┌────────┴─────────┐
                     │ config.json      │  ← admin のみが管理画面から編集
                     │ (判断基準ファイル) │
                     └──────────────────┘
```

### 3.1 パイプラインの3ステージ（設定テンプレートの3プロンプトと1:1対応）
1. **収集（Prompt-1 / クローリング）** → `raw_articles.json`
2. **フィルタリング（Prompt-2 / config.json適用）** → 中間xlsx（週次 or 月次）＋ 除外ログ
3. **描画（Prompt-3 / HTML生成）** → 週刊メルマガHTML or 月刊ビリーフHTML

### 3.2 config.json の役割（データフロー上の位置）
- Prompt-2 の **唯一の判断基準の正（source of truth）**。
- admin が管理画面でパラメータを変更 → `config.json` が更新される → **次回以降のフィルタリングが新基準で走る**（＝変更前と異なる結果になる）。
- ステージ2以外（収集・描画）は config を編集しない（読込のみ／またはメタ参照のみ）。

---

## 4. 用語定義

| 用語 | 定義 |
|---|---|
| 記事 (article) | クローリングで取得した1件のニュース単位。URL・タイトル・要約・出典・収集日を持つ。 |
| 事例 (case) | 月刊ビリーフの単位。企業/組織の具体的AI活用の1件。週次記事から昇格・再編集されることが多い。 |
| 情報カテゴリ | レポート全体の分類軸（7種）。記事1件に1つ（single）。 |
| 必須タグ | 記事に付与する10種のメタデータ。 |
| スコア | 6軸・100点満点の評価点。 |
| 除外ルール | 掲載対象外/低優先に落とす13種の基準。 |
| レポート採用区分 | 記事を出力形式へ振り分ける区分（例: 次回定例で提案／参考情報／共有のみ）。 |
| 除外ログ | 除外された記事の記録（重複検知の参照元にもなる）。 |
| ISO週 | `YYYY-Www` 形式（例 `2026-W31`）。 |

---

## 5. 判断基準ファイル `config.json` 仕様（★中核）

> **制約（顧客指定）**：`weekly_ai_intelligence_requirements.xlsx` は既存のフィルタリング基準ファイルである。**これをJSON化し、AIが読みやすい構造にする。** 以下は同xlsx（シート: `情報カテゴリ`/`必須タグ`/`除外ルール`/`スコアリング軸`）から起こした確定スキーマ。設計書ではこのJSONを正とし、xlsxは初期投入（マイグレーション）元として扱う。

### 5.1 設計原則
- **7カテゴリ・10タグ・6軸（合計100点）・13除外ルール** を漏れなく保持する。
- **管理者が変更する対象＝`tunable`（可変パラメータ）** と、**定義そのもの（enum/ID）＝ 原則固定** を明確に分離する。
  - 可変：スコア配点(`weight`)、採用しきい値、各除外ルールの `severity` と `enabled`、カテゴリの `priority`、週刊メルマガの対象業界。
  - 固定寄り：カテゴリID・タグID・軸ID（これらを変えると中間xlsxの互換が壊れるため、変更時は明示警告）。
- すべてのIDは英小文字スネークケース。日本語表示名は `label` に持つ。

### 5.2 完全な `config.json`（初期値。xlsx実データより生成）

```json
{
  "schema_version": "1.0",
  "meta": {
    "config_name": "ai_intelligence_requirements",
    "source_of_truth_xlsx": "weekly_ai_intelligence_requirements.xlsx",
    "editable_by": ["admin"],
    "visible_to": ["admin"],
    "updated_at": "2026-08-12T00:00:00+09:00",
    "updated_by": null,
    "revision": 1
  },

  "information_categories": [
    { "id": "ai_major_company_model", "label": "主要AI企業・モデル動向", "priority": "mid_high",
      "description": "主要企業、基盤モデル、生成AIプロダクト、AI基盤技術に関する重要な発表・変更" },
    { "id": "ai_agent_automation", "label": "AIエージェント・業務自動化", "priority": "high",
      "description": "AIエージェント、自律型AI、業務プロセス自動化、ワークフロー自動化に関する情報" },
    { "id": "ai_governance_risk", "label": "AIガバナンス・法規制・リスク", "priority": "high",
      "description": "AI利用ルール、法規制、セキュリティ、著作権、個人情報、リスク管理に関する情報" },
    { "id": "enterprise_ai_case", "label": "企業AI活用事例", "priority": "high",
      "description": "国内外企業によるAI導入・活用・業務改善・社内展開の具体事例" },
    { "id": "industry_ai_trend", "label": "業界別AI動向", "priority": "high",
      "description": "特定業界におけるAI活用、導入傾向、競争環境、業界課題との接続に関する情報" },
    { "id": "ai_training_org_change", "label": "AI人材育成・組織変革", "priority": "mid",
      "description": "AI研修、リスキリング、AI推進組織、CoE、社内展開、チェンジマネジメントに関する情報" },
    { "id": "ai_implementation_ops", "label": "AI導入・運用ノウハウ", "priority": "mid",
      "description": "AI導入、PoC、本番運用、評価、ROI、品質管理、定着化、内製化に関する実務情報" }
  ],

  "required_tags": [
    { "id": "information_category", "label": "情報カテゴリ", "type": "single", "required": true,
      "purpose": "レポート全体の分類軸", "value_source": "information_categories.id" },
    { "id": "ai_theme", "label": "AIテーマ", "type": "multi", "required": true,
      "purpose": "検索・絞り込みの中心", "value_source": "free_controlled" },
    { "id": "industry", "label": "業界", "type": "multi", "required": true,
      "purpose": "顧客別最適化に必須", "value_source": "enums.industry" },
    { "id": "business_area", "label": "業務領域", "type": "multi", "required": true,
      "purpose": "顧客の実務テーマと接続", "value_source": "enums.business_area" },
    { "id": "info_type", "label": "情報種別", "type": "single", "required": true,
      "purpose": "信頼性評価に必要", "value_source": "enums.info_type" },
    { "id": "region", "label": "地域", "type": "multi", "required": true,
      "purpose": "日本/海外/グローバルの区別", "value_source": "enums.region" },
    { "id": "reliability", "label": "信頼性", "type": "enum", "required": true,
      "purpose": "顧客向け掲載可否に影響", "value_source": "enums.reliability" },
    { "id": "customer_relevance", "label": "顧客関連度", "type": "enum", "required": true,
      "purpose": "顧客別レポートの中核", "value_source": "enums.customer_relevance" },
    { "id": "practical_usability", "label": "実務活用可能性", "type": "enum", "required": true,
      "purpose": "ニュースを示唆に変える", "value_source": "enums.practical_usability" },
    { "id": "adoption_class", "label": "レポート採用区分", "type": "enum", "required": true,
      "purpose": "3出力形式への振り分け", "value_source": "enums.adoption_class" }
  ],

  "scoring_axes": [
    { "id": "customer_relevance", "label": "顧客関連度", "weight": 25,
      "criterion": "対象企業の業界・業務・関心テーマにどれだけ関係するか",
      "bands": ["21-25:直接関係","16-20:近く応用可能","11-15:テーマ一部参考","6-10:一般参考","0-5:関連なし"] },
    { "id": "practical_usability", "label": "実務活用可能性", "weight": 20,
      "criterion": "顧客のAI推進・業務変革・ガバナンスに活用できるか",
      "bands": ["17-20:すぐ活用","13-16:具体例参考","9-12:追加解釈必要","5-8:一般的","0-4:見込み薄い"] },
    { "id": "market_impact", "label": "AI業界・市場インパクト", "weight": 20,
      "criterion": "AI業界全体、技術進化、企業活用に与える影響が大きいか",
      "bands": ["17-20:主要企業・技術進化","13-16:法人利用に影響","9-12:一定注目度","5-8:実務影響限定","0-4:不明確"] },
    { "id": "advisory_usability", "label": "アドバイザリー活用度", "weight": 15,
      "criterion": "定例会、提案、研修、議論テーマに使えるか",
      "bands": ["13-15:次回定例提案","10-12:共有・議論のきっかけ","7-9:補足情報","4-6:弱い","0-3:難しい"] },
    { "id": "reliability", "label": "信頼性", "weight": 10,
      "criterion": "情報源・内容の信頼性が高いか",
      "bands": ["9-10:公式/政府一次情報","7-8:主要媒体・専門媒体","5-6:ブログ・PR要確認","3-4:個人SNS二次情報","0-2:真偽不明"] },
    { "id": "urgency_freshness", "label": "緊急性・鮮度", "weight": 10,
      "criterion": "早めに共有・確認・対応すべき情報か",
      "bands": ["8-10:今週共有すべき重大情報","6-7:期限接近","3-5:参考","0-2:新規性低い"] }
  ],
  "scoring_total": 100,

  "exclusion_rules": [
    { "no": 1, "severity": "full_exclude", "enabled": true, "name": "真偽不明の噂・リーク・SNS単独情報",
      "examples": "X/Reddit/個人ブログのみ、公式確認なし、関係者によると、未発表機能の憶測" },
    { "no": 2, "severity": "full_exclude", "enabled": true, "name": "投資煽り・株価・銘柄推奨系ニュース",
      "examples": "AI関連銘柄急騰、今買うべきAI株、AIバブル、株価ランキング" },
    { "no": 3, "severity": "default_exclude", "enabled": true, "name": "アフィリエイト・広告色の強いツール紹介記事",
      "examples": "おすすめAIツール10選、代替ツールランキング、無料ツールまとめ、PR記事" },
    { "no": 4, "severity": "default_exclude", "enabled": true, "name": "生成AIを使ったエンタメ・バズコンテンツ",
      "examples": "AI美女、AIインフルエンサー、AIアート、AIミーム、個人作品紹介" },
    { "no": 5, "severity": "default_exclude", "enabled": true, "name": "個人向けアプリ・軽微な機能アップデート",
      "examples": "UI変更、スタンプ追加、テンプレート追加、個人サブスク変更" },
    { "no": 6, "severity": "default_exclude", "enabled": true, "name": "実務適用の見通しが薄い研究論文・ベンチマーク情報",
      "examples": "微妙なベンチマーク、数理モデルの細かな改良、実務示唆が弱い論文" },
    { "no": 7, "severity": "default_exclude", "enabled": true, "name": "海外スタートアップの小規模な資金調達ニュースのみ",
      "examples": "シード/Series A資金調達のみ、技術・事業内容が抽象的" },
    { "no": 8, "severity": "default_exclude", "enabled": true, "name": "AI要素の薄い一般DX・ITニュース",
      "examples": "クラウド移行、ERP刷新、SaaS導入のみ、タイトルだけAI" },
    { "no": 9, "severity": "default_exclude", "enabled": true, "name": "イベント・セミナー告知のみの情報",
      "examples": "AIセミナー開催告知、ウェビナー案内、展示会紹介、登壇者紹介のみ" },
    { "no": 10, "severity": "default_exclude", "enabled": true, "name": "採用・人事・組織変更のみのニュース",
      "examples": "役員人事、AI人材採用強化、部署新設、求人情報" },
    { "no": 11, "severity": "low_priority", "enabled": true, "name": "汎用的なTips記事",
      "examples": "ChatGPT活用Tips、プロンプト例、個人業務効率化ノウハウ" },
    { "no": 12, "severity": "merge", "enabled": true, "name": "重複・転載記事",
      "examples": "同一公式発表の複数記事、翻訳記事、内容ほぼ同一の記事" },
    { "no": 13, "severity": "low_priority_or_exclude", "enabled": true, "name": "古い情報の再掲・まとめ記事",
      "examples": "過去記事の再掲、同一日付は新しいが中身が古いまとめ" }
  ],

  "enums": {
    "priority": ["low", "mid", "mid_high", "high"],
    "severity": ["full_exclude", "default_exclude", "low_priority", "low_priority_or_exclude", "merge"],
    "reliability": ["高", "中", "要確認", "低"],
    "customer_relevance": ["直接関係", "近く応用可能", "テーマ一部参考", "一般参考", "関連薄い"],
    "practical_usability": ["すぐ活用", "具体例参考", "参考になる", "追加解釈が必要", "一般的", "見込み薄い"],
    "adoption_class": ["次回定例で提案", "参考情報", "共有のみ", "不採用"],
    "region": ["日本", "海外", "グローバル"],
    "info_type": ["一次情報(公式発表)", "主要メディア報道", "専門メディア報道", "ブログ・プレスリリース", "個人SNS・二次情報"],
    "industry": ["業界横断","不動産","製造","モビリティ・自動車","情報通信","IT","半導体","金融","小売","物流","エネルギー","メディア・エンタメ","公共","教育","医薬品","通信","ロボティクス","クラウド","航空宇宙"],
    "business_area": ["AI戦略","ガバナンス","法務・コンプライアンス","情報システム","セキュリティ","開発","業務プロセス改革","マーケティング","営業","カスタマーサポート","バックオフィス","人材育成・組織変革","研究開発","調達","データ基盤","生産・現場オペレーション","コンテンツ制作","経営企画"]
  },

  "tunable_thresholds": {
    "min_total_score_to_publish": 60,
    "adoption_class_score_map": {
      "propose_next_meeting": 85,
      "reference_info": 70,
      "share_only": 60
    },
    "min_reliability_score_to_publish": 5,
    "weekly": {
      "target_industry": "不動産",
      "max_industry_topics": 5,
      "max_common_topics": 6,
      "point_of_week_required": true
    },
    "monthly": {
      "target_case_count": 15,
      "chapter_count_hint": 5,
      "min_score_for_case": 80,
      "require_editorial_and_closing": true
    },
    "dedup": {
      "lookback_weeks": 8,
      "title_similarity_threshold": 0.85,
      "treat_same_url_as_duplicate": true
    }
  },

  "source_whitelist_hint": ["TechCrunch", "VentureBeat", "Ledge.ai", "ITmedia", "公式プレスリリース", "政府・公的機関"]
}
```

### 5.3 `priority` の記法補足
xlsx原本では「中〜高」という表記があるため、`mid_high` を enum に含める。表示時は `label` ではなく priority→日本語マップ（`high=高 / mid_high=中〜高 / mid=中 / low=低`）で描画する。

### 5.4 除外 severity の意味（フィルタ挙動）
| severity | 挙動 |
|---|---|
| `full_exclude` | 無条件で除外（除外ログに必ず記録）。 |
| `default_exclude` | 原則除外。ただし顧客関連度が「直接関係」かつ総合スコアがしきい値超なら例外採用可（要理由記載）。 |
| `low_priority` | 採用はするが `adoption_class` を下げる（`共有のみ`寄り）。 |
| `low_priority_or_exclude` | 鮮度が低ければ除外、そうでなければ低優先。 |
| `merge` | 除外ではなく統合。代表1件に集約し他はログへ（重複チェックと連動、§11）。 |

---

## 6. 権限管理仕様

### 6.1 要件
- `config.json` の **参照・編集は admin ロールのみ**。API・UI・ログのいずれにおいても、admin 以外に config の内容を返さない。
- 権限判定はサーバ側で行い、フロントの非表示制御のみに依存しない（クライアント側の隠蔽は補助）。
- config を変更する操作は監査ログに `who / when / diff(before→after) / revision` を残す。

### 6.2 権限マトリクス（API観点）
| 操作 | admin | editor | viewer | system |
|---|:--:|:--:|:--:|:--:|
| `GET /config` | ○ | 403 | 403 | 内部のみ |
| `PUT /config`（パラメータ更新） | ○ | 403 | 403 | × |
| `GET /reports/{period}`（HTML/一覧） | ○ | ○ | ○ | ○ |
| `POST /run/{weekly|monthly}`（実行） | ○ | ○ | 403 | ○ |
| `GET /config/history`（改訂履歴） | ○ | 403 | 403 | × |

### 6.3 config 更新の一貫性
- 更新は **楽観ロック**（`revision` で衝突検知）。
- 更新成功時に `revision++`、`updated_at`/`updated_by` を記録。
- 実行中ジョブは「実行開始時点の revision」を固定参照（実行中に config が変わっても途中で切り替わらない）。

---

## 7. 管理画面仕様（2アプリ共通のパラメータ編集）

### 7.1 要件（顧客指定）
- 管理者が **週刊メルマガ／月刊ビリーフのどちらのアプリを開いても**、共通の「判断基準パラメータ編集」画面に到達できる。
- 編集して保存すると `config.json` が書き換わり、**次回フィルタリングから新基準が適用**され、変更前と異なる結果になる。

### 7.2 編集可能パラメータ（§5.2 の可変項目にマップ）
| UI項目 | 対応 config パス | 種別 | 備考 |
|---|---|---|---|
| スコア軸の配点 | `scoring_axes[].weight` | 数値 | 合計100になるようバリデーション（±0強制 or 自動正規化を設計選択） |
| 掲載最低スコア | `tunable_thresholds.min_total_score_to_publish` | 数値0-100 | |
| 採用区分しきい値 | `tunable_thresholds.adoption_class_score_map.*` | 数値 | 降順整合チェック |
| 除外ルールの有効/無効 | `exclusion_rules[].enabled` | トグル | |
| 除外ルールの強度 | `exclusion_rules[].severity` | enum選択 | §5.4 |
| カテゴリ優先度 | `information_categories[].priority` | enum選択 | |
| 週刊：対象業界 | `tunable_thresholds.weekly.target_industry` | enum選択(industry) | 業界版の切替 |
| 週刊：トピック上限 | `weekly.max_industry_topics / max_common_topics` | 数値 | |
| 月刊：目標事例数・章数 | `monthly.target_case_count / chapter_count_hint` | 数値 | |
| 重複判定パラメータ | `dedup.*` | 数値/真偽 | §11 |

### 7.3 画面フロー
1. admin がアプリ（週刊 or 月刊）を開く → ナビに「判断基準（管理者）」タブが表示（admin のみ）。
2. タブを開くと現行 config をフォーム化して表示（`revision` を hidden 保持）。
3. 値を変更 → 「差分プレビュー」で before→after を表示。
4. 保存 → サーバでバリデーション＋楽観ロック → `config.json` 更新 → 監査ログ。
5. 任意で「この基準で再フィルタ（ドライラン）」を実行し、既存の収集済みデータに新基準を適用した結果件数を即プレビュー（実ファイルは上書きしない or バージョン付き出力）。

### 7.4 バリデーション
- `scoring_axes[].weight` の合計は 100（不一致は保存不可 or 自動正規化。設計書でどちらか選定）。
- `adoption_class_score_map` は `propose_next_meeting ≥ reference_info ≥ share_only ≥ min_total_score_to_publish`。
- ID系（category/tag/axis の `id`）はUIで編集不可（表示のみ）。変更が必要な場合は別の「スキーマ変更」画面で警告付きにする。

---

## 8. 中間レポートファイル仕様

> **制約（顧客指定）**：フィルタリング結果は、既存の `weekly_ai_intelligence_report.xlsx` / `monthly_ai_leading_cases.xlsx` と同形式の中間ファイルとして生成すること。HTML生成（Prompt-3）はこの中間ファイルを入力とする。

### 8.1 週刊：`weekly_ai_intelligence_report.xlsx`
- **シート構成**：ISO週ごとに1シート（例 `2026-W29`,`2026-W30`,`2026-W31`,`2026-W32`）＋ `除外ログ` シート。
- **各週シートの行構成**：
  - 1行目：タイトル `Weekly AI Intelligence レポート (2026-Www)`
  - 2行目：`要件定義書(weekly_ai_intelligence_requirements.xlsx)のルールに基づき分類・採点。合計スコア降順。`
  - 3行目：空行
  - 4行目：ヘッダ（下記22列）
  - 5行目以降：記事データ（**合計スコア降順**）
- **列定義（22列・順序厳守）**：

| # | 列名 | 内容 | config対応 |
|--:|---|---|---|
| 1 | 収集日 | `YYYY-MM-DD` | |
| 2 | 情報カテゴリ | カテゴリID（英） | information_categories.id |
| 3 | タイトル | 記事タイトル | |
| 4 | 一言要約 | 2〜3文の要約 | |
| 5 | 合計スコア | 0-100 | scoring_total |
| 6 | 緊急性鮮度_点 | 0-10 | urgency_freshness |
| 7 | 信頼性_点 | 0-10 | reliability |
| 8 | アドバイザリー活用度_点 | 0-15 | advisory_usability |
| 9 | AI業界市場インパクト_点 | 0-20 | market_impact |
| 10 | 実務活用可能性_点 | 0-20 | practical_usability |
| 11 | 顧客関連度_点 | 0-25 | customer_relevance |
| 12 | レポート採用区分 | enum | adoption_class |
| 13 | 実務活用可能性 | enum | practical_usability |
| 14 | 顧客関連度 | enum | customer_relevance |
| 15 | 信頼性 | enum | reliability |
| 16 | 地域 | multi（`;`区切り） | region |
| 17 | 情報種別 | enum | info_type |
| 18 | 業務領域 | multi（`;`区切り） | business_area |
| 19 | 業界 | multi（`;`区切り） | industry |
| 20 | AIテーマ | multi（`;`区切り） | ai_theme |
| 21 | ソース | 媒体名（統合時 `A / B(統合)`） | |
| 22 | URL | 記事URL | |

- **`除外ログ` シート（6列）**：`収集日 / タイトル / URL / ソース / 除外区分 / 除外理由`。
  - `除外区分` は severity の日本語（完全除外／原則除外／低優先／統合 等）。
  - 重複チェック（§11）の参照元。

### 8.2 月刊：`monthly_ai_leading_cases.xlsx`
- **シート構成**：対象月ごとに1シート（例 `2026-07`）。
- **列定義（8列・順序厳守）**：

| # | 列名 | 内容 |
|--:|---|---|
| 1 | No | 通し番号（1〜） |
| 2 | トピック(章) | `第N章 <章タイトル>` |
| 3 | 企業・組織 | 主体（複数可 `A・B`） |
| 4 | タイトル | 事例見出し |
| 5 | URL | 一次/報道URL |
| 6 | 出典 | `媒体（日付）／ プレスリリース` 形式 |
| 7 | 掲載月 | `YYYY-MM` |
| 8 | 解説 | 事例本文。段落は `\n\n` 区切りで3段落構成（①事実 ②詳細 ③示唆/持ち帰り）を推奨 |

- 章（トピック）は複数事例をまたいで共有。同一章の事例は連続配置。

---

## 9. 週刊メルマガ HTML 仕様

> **制約（顧客指定）**：体裁は `weekly_ai_intelligence_newsletter_不動産_2026-W31.html` に準拠する。以下は同ファイルから抽出した構造。**メールHTML（table レイアウト＋inline style）**である点を厳守（`<style>`タグやFlex/Grid不可）。

### 9.1 全体
- 外枠：`max-width:680px` 中央寄せ。背景 `#f3f4f6`。
- フォント：`'Hiragino Kaku Gothic ProN','ヒラギノ角ゴ ProN','Meiryo',Arial,sans-serif`。
- 配色（週刊のブランド）：インディゴ〜バイオレットのグラデーション `linear-gradient(135deg,#4f46e5,#7c3aed)`、アクセント `#4f46e5`、示唆ボックス `#eef2ff`＋左罫 `#6366f1`。

### 9.2 セクション構成（上から）
1. **ヘッダ**：グラデ背景。`Weekly AI Intelligence by Sapeet` / `<業界>版`（例「不動産 版」）/ `対象週：2026-Www`。業界名は `config.weekly.target_industry` から。
2. **今週のポイント**：白カード。当週の総括3〜4文（業界視点の要旨）。`config.weekly.point_of_week_required=true` の場合は必須。
3. **業界関連トピック**（`<業界>関連トピック`）：対象業界に直接効く記事。「その他ピックアップ」として `<ul>` のリンク列挙形式（見出し＋媒体名）でも可。件数上限 `weekly.max_industry_topics`。
4. **業界共通トピック**：全業界共通で重要な記事カード群。件数上限 `weekly.max_common_topics`。各カードは以下の要素：
   - カテゴリラベル（小・色分け。例「AIエージェント・自動化」＝シアン、「主要企業・モデル動向」＝バイオレット、「ガバナンス・リスク」＝レッド）
   - タイトル（`<a>` リンク・黒文字）
   - 本文要約（`一言要約` を流用可）
   - **示唆ボックス**（`#eef2ff` 背景・左罫 `#6366f1`）：「自社ではどう捉えるか」の1段落。
   - 出典行（`出典：媒体 ／ 記事を読む`）
5. **フッタ**：注記（編集部整理であり投資・法務助言でない旨）。

### 9.3 記事→カードの対応
- 中間xlsx（当週シート）から `レポート採用区分 ≠ 不採用` かつ `合計スコア ≥ min_total_score_to_publish` の記事を採用。
- 業界振り分け：`業界` に `target_industry` を含む→業界関連トピック、`業界横断` 等→業界共通トピック。
- カテゴリラベルの色は情報カテゴリIDにマップ（設計書で色マップを定義。サンプル準拠）。
- 並び順：合計スコア降順。

---

## 10. 月刊ビリーフ HTML 仕様

> **制約（顧客指定）**：体裁は `monthly_belief_2026-07.html` に準拠する。以下は同ファイルから抽出した構造。こちらもメールHTML（table＋inline style）。

### 10.1 全体
- 外枠：`width:680px; max-width:100%`、角丸 `8px`、`box-shadow`。背景 `#EEF2F6`、本体白。
- 配色（月刊のブランド）：ネイビー `#1F4E78`（ヘッダ/章ラベル/フッタ）、アクセント水色 `#4FA8DB`／`#9FD4F2`、本文 `#2C3E50`、囲み `#F7FAFC`＋罫 `#DCE7F0`。
- フォント：週刊と同系（ヒラギノ/メイリオ）。

### 10.2 セクション構成（上から）
1. **ヘッダ**：`MONTHLY REPORT ON LEADING AI CASES` / `月刊ビリーフ by Sapeet` / `2026年7月号` バッジ / `対象期間：YYYY年M月1日 〜 M月末日` / 説明1文。
2. **巻頭言（EDITORIAL）**：見出し「巻頭言 ― 今月の総論」＋サブ見出し（当月を一言で表す命題。例「『導入したか』ではなく『作り直したか』が問われ始めた月」）。本文は `#F7FAFC` カードに3段落程度。当月全事例を俯瞰する総論。
3. **目次（CONTENTS）**：ネイビーカード。章一覧（`第N章` バッジ＋章タイトル＋`件数`右寄せ）。末尾に `全N事例・M章`。
4. **本編（章 × 事例）**：章ごとに
   - 章ヘッダ：`第N章` バッジ＋章タイトル＋章導入文（下端に `2px solid #4FA8DB` 罫）。
   - 事例カード（`CASE NN ／ 企業名`）：タイトル（`<a>` リンク・ネイビー）＋本文3〜4段落（①事実 ②詳細 ③④示唆）＋出典行（上罫＋グレー小文字）。
5. **むすび（CLOSING）**：見出し「むすび ― 来月への視点」＋ `#F7FAFC` カードに2段落（今月の総括＋来月の視点）。
6. **フッタ**：ネイビー。`月刊ビリーフ by Sapeet ／ YYYY年M月号`、`収録事例 N 件`／`トピック M 章` バッジ、対象期間・発行日。必要に応じ「特定媒体は一般解説中心のため個別事例採用なし」等の注記。

### 10.3 事例→カードの対応
- 入力は `monthly_ai_leading_cases.xlsx` の当月シート。`No` 昇順・章グルーピング順に配置。
- `解説` の `\n\n` 段落を `<p>` に分割。最終段落は「示唆／持ち帰り」トーンにする（サンプル準拠）。
- `企業・組織` を `CASE NN ／ <企業>` に、`タイトル` をリンク見出しに、`出典` を出典行に割当。
- `target_case_count`（既定15）と `chapter_count_hint`（既定5）を目安に構成。

---

## 11. 重複チェック仕様

> **制約（顧客指定）**：既存の「除外ログ」と同様の考え方で、既出記事・既出事例との重複を検知する。

### 11.1 対象
- **週次**：直近 `dedup.lookback_weeks`（既定8週）分の各週シート＋除外ログを参照。
- **月次**：当月＋直近数ヶ月の `monthly_ai_leading_cases.xlsx` と、対応する週次記事を参照。

### 11.2 判定ロジック
1. **URL一致**：`treat_same_url_as_duplicate=true` の場合、正規化URL（クエリ・トラッキング除去、末尾スラッシュ統一）が既出と一致 → 重複。
2. **タイトル類似**：正規化後（記号・空白除去、全半角統一）に類似度 ≥ `title_similarity_threshold`（既定0.85）→ 重複候補。
3. **同一発表の別媒体**：同一の一次発表を指す複数記事は severity `merge` として代表1件へ統合、残りは除外ログに `除外区分=統合 / 除外理由=重複・転載記事`。

### 11.3 出力
- 重複と判定された記事は本編に載せず、**除外ログ**に必ず記録（`収集日/タイトル/URL/ソース/除外区分/除外理由`）。
- 統合時は代表記事の `ソース` 欄に `A / B(統合)` の形で併記（既存samplesに準拠）。

---

## 12. スコアリング根拠フォーマットチェック仕様

> **制約（顧客指定）**：スコアリング根拠の必須項目記載漏れを検知する。

### 12.1 必須項目（1記事あたり）
- 6軸すべての **点数**（範囲内：顧客関連度0-25／実務20／市場20／アドバイザリー15／信頼性10／緊急性10）。
- 6軸点の合計＝ `合計スコア` と一致（不一致は要修正）。
- 10タグ（`required:true`）すべてが埋まっている（`information_category / ai_theme / industry / business_area / info_type / region / reliability / customer_relevance / practical_usability / adoption_class`）。
- enum系タグの値が config の `enums` に存在する（未定義値はエラー）。
- `一言要約` が空でない。`URL`・`ソース`・`収集日` が空でない。

### 12.2 チェック挙動
- 週次フィルタリング（Prompt-2）完了時に自動実行。
- 結果は「検証レポート（`validation_YYYY-Www.json`）」として出力：`{ ok: bool, errors: [{row, field, reason}], warnings: [...] }`。
- **エラーがある記事は本編HTML生成の対象から除外**し、除外ログに `除外区分=フォーマット不備` として記録。
- 合計スコア不一致・enum外・タグ欠落は `error`、要約が短すぎる等は `warning`。

---

## 13. スケジュールタスク／設定テンプレート（3プロンプト）

> **制約（顧客指定）**：週次・月次の実行をスケジュールタスク化する。設定テンプレートは3プロンプト：①クローリング収集 ②config適用フィルタ ③HTML生成。各段で中間ファイル（`weekly_..._report.xlsx` / `monthly_ai_leading_cases.xlsx` 相当）が生成されること。

### 13.1 スケジュール設定テンプレート（YAML例）
設計書ではこれを cron/ジョブ定義に落とす。**プロンプト本文は §13.2〜13.4 を参照**。

```yaml
schedules:
  - id: weekly_ai_intelligence
    cron: "0 8 * * MON"          # 毎週月曜 08:00 JST
    timezone: "Asia/Tokyo"
    period: "{{ISO_WEEK}}"        # 例 2026-W31
    pipeline:
      - step: crawl
        prompt_ref: PROMPT-1
        output: "raw_articles_{{ISO_WEEK}}.json"
      - step: filter
        prompt_ref: PROMPT-2
        config: "config.json@revision"     # 実行開始時のrevisionを固定
        input: "raw_articles_{{ISO_WEEK}}.json"
        outputs:
          - "weekly_ai_intelligence_report.xlsx#sheet={{ISO_WEEK}}"
          - "weekly_ai_intelligence_report.xlsx#sheet=除外ログ (append)"
          - "validation_{{ISO_WEEK}}.json"
      - step: render
        prompt_ref: PROMPT-3-WEEKLY
        input: "weekly_ai_intelligence_report.xlsx#sheet={{ISO_WEEK}}"
        template: "weekly_ai_intelligence_newsletter_<industry>_<ISO_WEEK>.html"
        output: "weekly_ai_intelligence_newsletter_{{target_industry}}_{{ISO_WEEK}}.html"

  - id: monthly_belief
    cron: "0 9 1 * *"            # 毎月1日 09:00 JST（前月分を生成）
    timezone: "Asia/Tokyo"
    period: "{{PREV_MONTH}}"      # 例 2026-07
    pipeline:
      - step: crawl
        prompt_ref: PROMPT-1
        output: "raw_articles_{{PREV_MONTH}}.json"
      - step: filter
        prompt_ref: PROMPT-2
        config: "config.json@revision"
        input: "raw_articles_{{PREV_MONTH}}.json"
        outputs:
          - "monthly_ai_leading_cases.xlsx#sheet={{PREV_MONTH}}"
          - "validation_{{PREV_MONTH}}.json"
      - step: render
        prompt_ref: PROMPT-3-MONTHLY
        input: "monthly_ai_leading_cases.xlsx#sheet={{PREV_MONTH}}"
        template: "monthly_belief_<MONTH>.html"
        output: "monthly_belief_{{PREV_MONTH}}.html"
    notes: "月次のfilterは、対象月の各週次レポートを再利用してもよい（再クロール省略可）。"
```

### 13.2 【PROMPT-1】クローリング収集プロンプト（AIがWeb情報収集）
> 目的：対象期間のAI関連ニュースを広く収集し、`raw_articles.json` に構造化する。取捨選択（除外・採点）はここでは行わない（次段の責務）。

```
あなたはAI動向のリサーチャーです。対象期間 {{PERIOD}}（週次なら該当ISO週、月次なら該当月）に公開されたAI関連ニュースを収集してください。

【収集対象の優先ソース】
TechCrunch / VentureBeat / Ledge.ai / ITmedia / 各社公式プレスリリース / 政府・公的機関発表。
その他でも信頼できる主要・専門メディアは可。個人ブログ・SNS単独・まとめアフィリエイトは収集しない。

【収集範囲の観点（config.jsonの7カテゴリを網羅するよう広く）】
- 主要AI企業・モデル動向 / AIエージェント・業務自動化 / AIガバナンス・法規制・リスク /
  企業AI活用事例 / 業界別AI動向 / AI人材育成・組織変革 / AI導入・運用ノウハウ
- 週次の場合は「今週」の新規性を重視。月次の場合は「先進企業の具体的活用事例」を重視。

【この段階でやらないこと】
- スコアリング・除外判定・タグ確定はしない（次段の責務）。ここは網羅的に集めることに徹する。

【出力：raw_articles.json（配列）】各記事に以下を必ず含める：
{
  "collected_at": "YYYY-MM-DD",       // 収集日
  "published_at": "YYYY-MM-DD|null",  // 公開日（わかる範囲）
  "title": "記事タイトル",
  "url": "https://...",               // 正規化前でよい
  "source": "媒体名",
  "raw_summary": "本文から2〜4文の客観要約（意見を混ぜない）",
  "region_hint": "日本|海外|グローバル|不明",
  "primary_or_secondary": "一次(公式)|報道|不明"
}
重複しうる記事もこの段階では落とさず全て残す（次段で統合判定する）。
JSON以外は出力しない。
```

### 13.3 【PROMPT-2】フィルタリング／分類・採点プロンプト（config.json適用）
> 目的：`raw_articles.json` を config.json のルールで分類・10タグ付与・6軸採点・13除外・重複統合し、中間xlsx（週次 or 月次）と除外ログ・検証レポートを生成する。

```
あなたはAI動向レポートの編集アナリストです。判断基準ファイル config.json を唯一の基準として、
raw_articles.json の各記事を評価・分類してください。config.json のパラメータ（配点・しきい値・除外の有効/強度・対象業界）は
実行時点の値をそのまま使用し、あなたの主観で上書きしないこと。

■ 入力
- config.json（判断基準。7カテゴリ / 10必須タグ / 6軸100点 / 13除外ルール / tunable_thresholds）
- raw_articles.json（収集生データ）
- 過去データ（重複判定用）：直近 {{dedup.lookback_weeks}} 週の weekly_report.xlsx 各週シート＋除外ログ（月次は直近数ヶ月のcases）

■ 手順（各記事につき）
1. 除外判定（exclusion_rules を No 順に評価）
   - severity=full_exclude に該当 → 除外（理由=ルール名）。
   - severity=default_exclude に該当 → 原則除外。ただし顧客関連度=「直接関係」かつ合計見込み ≥ min_total_score_to_publish なら例外採用（理由を明記）。
   - low_priority / low_priority_or_exclude / merge は §除外severity定義に従う。
2. 重複・統合判定
   - URL正規化一致、またはタイトル類似度 ≥ {{dedup.title_similarity_threshold}} を重複とみなす。
   - 同一発表の別媒体は代表1件へ統合（ソース欄に「A / B(統合)」）。残りは除外ログへ（除外区分=統合）。
3. 分類・タグ付与（10必須タグを全て埋める。enum系は config.enums の値のみ使用）
   - information_category(1つ) / ai_theme(複数) / industry(複数) / business_area(複数) /
     info_type / region(複数) / reliability / customer_relevance / practical_usability / adoption_class
4. 6軸採点（各軸 bands に従い整数配点。範囲厳守）
   - 顧客関連度0-25 / 実務活用可能性0-20 / AI業界市場インパクト0-20 / アドバイザリー活用度0-15 / 信頼性0-10 / 緊急性鮮度0-10
   - 合計スコア = 6軸の和（必ず一致させる）。
   - adoption_class は adoption_class_score_map に従って決定（propose_next_meeting/reference_info/share_only/不採用）。
5. 採否
   - 合計スコア < min_total_score_to_publish、または信頼性点 < min_reliability_score_to_publish → 除外ログへ（除外区分=低スコア/信頼性不足）。

■ フォーマットチェック（§12）
- 6軸点が全て範囲内か・和が合計と一致するか・10必須タグが全て埋まっているか・enum外の値がないか・URL/ソース/収集日/一言要約が空でないかを検証。
- 不備は validation レポートに error として記録し、その記事は本編対象から外す（除外区分=フォーマット不備）。

■ 出力
1) 中間xlsx（週次: weekly_ai_intelligence_report.xlsx の {{PERIOD}} シート、22列・合計スコア降順／
   月次: monthly_ai_leading_cases.xlsx の {{PERIOD}} シート、8列・章グルーピング）。列順は仕様§8に厳密準拠。
   ※月次では、採用記事のうち「企業・組織の具体的活用事例」を事例(case)へ昇格し、章(トピック)を5前後に束ねる。各事例の「解説」は
     ①事実 ②詳細 ③示唆(持ち帰り) の3段落を \n\n 区切りで書く。
2) 除外ログ（append）：収集日/タイトル/URL/ソース/除外区分/除外理由（6列）。
3) validation_{{PERIOD}}.json：{ ok, errors[], warnings[] }。
中間xlsx・ログ・検証以外の余計な出力はしない。
```

### 13.4 【PROMPT-3】HTML生成プロンプト（中間xlsx → 定型HTML）
中間xlsxを入力に、体裁テンプレに厳密準拠したHTMLを生成する。**週刊・月刊で別プロンプト**。

#### 13.4.1 PROMPT-3-WEEKLY（週刊メルマガHTML）
```
あなたはニュースレター編集者です。weekly_ai_intelligence_report.xlsx の {{ISO_WEEK}} シート（採用記事のみ）を入力に、
週刊メルマガHTMLを生成してください。体裁は weekly_ai_intelligence_newsletter_不動産_2026-W31.html に厳密準拠。

【厳守事項（メールHTML）】
- table レイアウト＋inline style のみ。<style>タグ/外部CSS/flex/grid/JS 禁止。max-width:680px 中央。
- フォント: 'Hiragino Kaku Gothic ProN','Meiryo',Arial,sans-serif。
- 配色: ヘッダ グラデーション linear-gradient(135deg,#4f46e5,#7c3aed)、アクセント#4f46e5、示唆ボックス背景#eef2ff/左罫#6366f1。

【構成（上から）】
1. ヘッダ: 「Weekly AI Intelligence by Sapeet」/「{{target_industry}} 版」/「対象週：{{ISO_WEEK}}」
2. 今週のポイント（白カード, 3〜4文の業界視点総括）
3. {{target_industry}}関連トピック: 業界に「業界」タグが一致する記事。「その他ピックアップ」の<ul>リンク列挙可。上限 {{weekly.max_industry_topics}} 件。
4. 業界共通トピック: 「業界横断」等の記事カード群。上限 {{weekly.max_common_topics}} 件。各カード=
   カテゴリラベル（色分け）/ タイトル<a>リンク / 一言要約 / 示唆ボックス（自社視点1段落）/ 出典行「出典：{{ソース}} ／ 記事を読む」
5. フッタ: 「編集部整理であり投資・法務助言ではない」旨の注記。

【並び順】合計スコア降順。【カテゴリ色マップ】ai_agent_automation=#0891b2, ai_major_company_model=#7c3aed, ai_governance_risk=#dc2626 …（サンプル準拠、他は設計書の色マップに従う）。
【リンク】各カードの<a> href は xlsx の URL 列をそのまま使用。
HTML以外は出力しない。
```

#### 13.4.2 PROMPT-3-MONTHLY（月刊ビリーフHTML）
```
あなたは月刊特集の編集者です。monthly_ai_leading_cases.xlsx の {{MONTH}} シートを入力に、
月刊ビリーフHTMLを生成してください。体裁は monthly_belief_2026-07.html に厳密準拠。

【厳守事項（メールHTML）】
- table レイアウト＋inline style のみ。<style>/flex/grid/JS 禁止。width:680px; max-width:100%; 角丸8px; 中央。
- フォント: ヒラギノ/メイリオ系。配色: ネイビー#1F4E78 / アクセント#4FA8DB・#9FD4F2 / 本文#2C3E50 / 囲み#F7FAFC＋罫#DCE7F0。

【構成（上から）】
1. ヘッダ: 「MONTHLY REPORT ON LEADING AI CASES」/「月刊ビリーフ by Sapeet」/「{{MONTH_JP}}号」バッジ/「対象期間：{{month_range}}」/説明1文。
2. 巻頭言(EDITORIAL): 「巻頭言 ― 今月の総論」＋当月を一言で表すサブ見出し＋#F7FAFCカードに総論3段落（全事例を俯瞰）。
3. 目次(CONTENTS): ネイビーカード。章一覧（「第N章」バッジ＋章タイトル＋件数右寄せ）＋「全{{N}}事例・{{M}}章」。
4. 本編: 章ごとに [章ヘッダ（第N章バッジ＋章タイトル＋導入文, 下端2px#4FA8DB罫）] → [事例カード群]。
   事例カード = 「CASE NN ／ {{企業・組織}}」ラベル / {{タイトル}}を<a>リンク見出し(ネイビー) /
   {{解説}}を\n\n段落で<p>分割（3〜4段落, 最終段は示唆トーン）/ 出典行「出典：{{出典}}」(上罫・グレー小)。
5. むすび(CLOSING): 「むすび ― 来月への視点」＋#F7FAFCカードに2段落（今月総括＋来月視点）。
6. フッタ: ネイビー。「月刊ビリーフ by Sapeet ／ {{MONTH_JP}}号」/「収録事例 {{N}} 件」「トピック {{M}} 章」バッジ/対象期間・発行日。

【並び順】xlsx の No 昇順＝章グルーピング順。件数目安は target_case_count({{monthly.target_case_count}})・chapter_count_hint({{monthly.chapter_count_hint}})。
【リンク】各事例の<a> href は URL 列。
HTML以外は出力しない。
```

---

## 14. 非機能要件・エラー処理

- **冪等性**：同一 `{{PERIOD}}` の再実行は該当シート/HTMLを上書き（バージョン退避オプションを設計書で選定）。
- **config固定**：ジョブは開始時 revision を固定参照（§6.3）。
- **失敗時**：各ステップは独立に再実行可能（`raw_articles.json` があれば crawl をスキップして filter から再開できる）。
- **監査**：config 変更・実行・生成物を監査ログに記録。
- **文字コード**：入出力すべて UTF-8。HTMLは `<meta charset>` 明記。
- **PII/著作権**：本文の長文転載はしない（要約は自作文）。出典URL・媒体名は必ず保持。
- **タイムゾーン**：`Asia/Tokyo` 基準。ISO週は月曜始まり。

---

## 15. 設計書に落とし込む際の指示（次のClaudeへ）

このセクションはあなた（設計書を書くClaude）への指示です。本仕様書を入力として、以下を含む**設計書**を作成してください。

1. **アーキテクチャ設計**：§3のパイプラインを、コンポーネント図＋シーケンス図（crawl→filter→render、config読込、権限判定、スケジュール起動）で表現。
2. **データモデル**：
   - `config.json` のJSON Schema（§5.2を正とし、tunable/固定を型で表現、バリデーション規則§7.4を制約として記述）。
   - 中間xlsxの列スキーマ（§8。22列/8列/除外ログ6列）をテーブル定義として明文化。
   - `raw_articles.json` / `validation_*.json` のスキーマ。
3. **API設計**：§6.2の権限マトリクスを満たすエンドポイント定義（`GET/PUT /config`, `GET /config/history`, `GET /reports/{period}`, `POST /run/{type}`）。認可はサーバ側。
4. **権限・認可設計**：ロール（admin/editor/viewer/system）とアクセス制御（config は admin のみ）。監査ログのスキーマ。楽観ロック（revision）。
5. **管理画面設計**：§7の編集フォーム（configパスへのバインディング）、差分プレビュー、ドライラン再フィルタ、バリデーションUI。**週刊/月刊どちらのアプリからも同一の管理画面に到達**できる導線。
6. **フィルタリング設計**：§13.3の手順をアルゴリズム（擬似コード）化。除外severityの分岐、重複統合、採点、adoption_class決定、フォーマットチェック（§12）。
7. **HTML生成設計**：§9/§10のセクションを、中間xlsx列→HTML要素へのマッピング表として定義。メールHTML制約（table+inline style, <style>禁止）を明記。カテゴリ色マップを確定。
8. **スケジューラ設計**：§13.1のYAMLをジョブ定義に変換。ステップ間の中間成果物受け渡し、再開ポイント、冪等性。
9. **プロンプト運用設計**：§13.2〜13.4の3プロンプトをテンプレート化し、`{{変数}}` の注入元（config値・期間）を明記。プロンプトのバージョン管理方針。
10. **移行設計**：`weekly_ai_intelligence_requirements.xlsx` → `config.json` への初期マイグレーション手順。

> **設計判断を委ねる点（設計書内で選定・明記すること）**
> - scoring_axes.weight 合計100の担保方法（保存拒否 or 自動正規化）。
> - 中間ファイル上書き vs バージョン退避。
> - ドライラン再フィルタの結果を一時ファイルに出すか、メモリ上プレビューに留めるか。
> - config編集を「アプリ内」で行うか「管理専用サブ画面」に分けるか（ただし admin 限定は必須）。

以上。
