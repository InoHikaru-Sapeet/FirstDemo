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
	 * ⚠️ **業界はキーに含めない**（T-52 Step 1 で業界版を廃止した。週刊は
	 * 業界を問わない1本なので、号を決めるのは period だけ）。
	 */
	articles: (period: string) => ["reports", period, "articles"] as const
};

/** 判断基準（T-33・T-34・T-35）。 */
export const configKeys = {
	all: ["config"] as const,
	/** `GET /config`（admin のみ）。 */
	current: () => ["config"] as const,
	/** `GET /config/history`（admin のみ）。 */
	history: () => ["config", "history"] as const
};
