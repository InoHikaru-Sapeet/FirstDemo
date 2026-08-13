import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { isUnauthorized } from "@/api/client";
import { authKeys } from "@/api/query-keys";

/**
 * `QueryClient` を1つ作る。
 *
 * テストが毎回まっさらな状態で**アプリと同じ 401 の扱い**を得られるように、
 * シングルトンではなくファクトリを公開している。
 */
export function createQueryClient(): QueryClient {
	/**
	 * セッション失効時に自動でログイン画面へ落とす（T-43 完了条件）。
	 *
	 * 任意のクエリ／ミューテーションが 401 を返したら、それは**セッションが
	 * 切れた**ということ（ログイン済みで権限が無いだけなら 403 が返る）。
	 * `['auth','me']` を `null` に上書きすると `RequireAuth` が未ログインと
	 * 判断してリダイレクトする。
	 *
	 * ⚠️ `invalidateQueries` にしないのは、401 の時点でセッションが無効だと
	 * 確定しているため。再取得を待つ間だけ「ログイン済み」の画面が残るのを避ける。
	 */
	const handleUnauthorized = (error: unknown): void => {
		if (!isUnauthorized(error)) {
			return;
		}
		client.setQueryData(authKeys.me(), null);
	};

	const client = new QueryClient({
		queryCache: new QueryCache({ onError: handleUnauthorized }),
		mutationCache: new MutationCache({ onError: handleUnauthorized }),
		defaultOptions: {
			queries: {
				retry: 1,
				staleTime: 30_000
			}
		}
	});

	return client;
}

export const queryClient = createQueryClient();
