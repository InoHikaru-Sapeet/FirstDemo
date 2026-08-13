import { useQuery } from "@tanstack/react-query";
import { type CurrentUser, fetchCurrentUser } from "@/api/auth";
import { authKeys } from "@/api/query-keys";

/**
 * ログイン中の利用者とロールを供給する（T-43）。
 *
 * これが T-32（ナビの出し分け）・T-36（実行ボタンの出し分け）の入力になる。
 * ⚠️ **フロントでの出し分けは補助**であって、権限の実体はサーバー側の判定
 * （§6.1・§6.2）。非表示にしただけの操作が API で通ってはいけない。
 */
export type UseCurrentUserResult = {
	/** 未ログインなら `null`。読み込み中も `null`（`isLoading` で区別する）。 */
	user: CurrentUser | null;
	isLoading: boolean;
	isAuthenticated: boolean;
};

export function useCurrentUser(): UseCurrentUserResult {
	const query = useQuery({
		queryKey: authKeys.me(),
		queryFn: fetchCurrentUser,
		// ⚠️ リトライしない。未ログイン（401 → `null`）は正常な答えなので、
		// 再試行しても結果は変わらずログイン画面の表示が遅れるだけ。
		retry: false
	});

	const user = query.data ?? null;
	return {
		user,
		isLoading: query.isPending,
		isAuthenticated: user !== null
	};
}
