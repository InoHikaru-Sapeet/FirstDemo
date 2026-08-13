/**
 * 「ログイン後に元の URL へ戻す」ための受け渡し（T-43 完了条件）。
 *
 * 行き先は react-router の location state で運ぶ。クエリ文字列に入れると
 * オープンリダイレクトの検査が必要になるうえ、履歴に残って邪魔になる。
 */

export const LOGIN_PATH = "/login";
export const REGISTER_PATH = "/register";

/** ログイン後の既定の着地先。自己登録直後の viewer もここに来る（T-36）。 */
export const DEFAULT_AUTHENTICATED_PATH = "/";

export type LoginRedirectState = {
	from: string;
};

/** 現在地から「戻り先」を組み立てる。 */
export function buildLoginRedirectState(location: {
	pathname: string;
	search: string;
}): LoginRedirectState {
	return { from: `${location.pathname}${location.search}` };
}

/**
 * location state から戻り先を取り出す。
 *
 * state は任意の値が入りうる（利用者が履歴を操作できる）ので型ガードで確かめる。
 * ⚠️ **アプリ内のパスだけを許す。** `//evil.example` や `https://…` を通すと
 * ログイン画面が外部サイトへの踏み台になる（オープンリダイレクト）。
 */
export function readLoginRedirectTarget(state: unknown): string {
	if (
		typeof state !== "object" ||
		state === null ||
		!("from" in state) ||
		typeof state.from !== "string"
	) {
		return DEFAULT_AUTHENTICATED_PATH;
	}

	const from = state.from;
	if (!from.startsWith("/") || from.startsWith("//")) {
		return DEFAULT_AUTHENTICATED_PATH;
	}
	// ログイン画面自身へ戻すと往復になる。
	if (from === LOGIN_PATH || from === REGISTER_PATH) {
		return DEFAULT_AUTHENTICATED_PATH;
	}
	return from;
}
