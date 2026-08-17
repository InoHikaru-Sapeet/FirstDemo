/**
 * TanStack Query のクエリキー規約（T-31 の完了条件。T-43 で使う分から作る）。
 *
 * キーを文字列リテラルで散らすと `invalidateQueries` の対象がずれるので、
 * **必ずここ経由**で組み立てる。T-31・T-33 以降で `config` / `reports` を足す
 * （`['config']` / `['config','history']` / `['reports', period]`）。
 */
export const authKeys = {
	all: ["auth"] as const,
	/** `GET /auth/me`。ログイン状態の唯一の情報源。 */
	me: () => ["auth", "me"] as const
};

/** レポート閲覧（T-36）。 */
export const reportKeys = {
	all: ["reports"] as const,
	/** `GET /reports`（一覧）。 */
	list: () => ["reports", "list"] as const,
	/** `GET /reports/{period}`。 */
	detail: (period: string) => ["reports", period] as const,
	/**
	 * `GET /reports/{period}/articles`。
	 *
	 * ⚠️ **業界をキーに含める**（業界版ごとに別の内容＝今週のポイントも示唆も
	 * 変わる）。含めないと業界を切り替えても前の版が表示され続ける。
	 */
	articles: (period: string, industry: string | null) =>
		["reports", period, "articles", industry] as const
};

/** 判断基準（T-33・T-34・T-35）。 */
export const configKeys = {
	all: ["config"] as const,
	/** `GET /config`（admin のみ）。 */
	current: () => ["config"] as const,
	/** `GET /config/history`（admin のみ）。 */
	history: () => ["config", "history"] as const
};
