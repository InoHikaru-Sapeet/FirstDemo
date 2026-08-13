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
