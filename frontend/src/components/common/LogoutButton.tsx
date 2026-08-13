import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { logout } from "@/api/auth";
import { authKeys } from "@/api/query-keys";
import { Button } from "@/components/ui/button";
import { LOGIN_PATH } from "@/utils/loginRedirect";

/**
 * ログアウト導線（T-43 完了条件）。
 *
 * ⚠️ Cookie を消すだけでは終わらない。`POST /auth/logout` で**サーバー側の
 * セッションを失効**させるのが本体（Cookie のコピーを取られていても使えなくなる）。
 * 失敗しても手元のキャッシュは捨ててログイン画面へ送る（サーバーで既に失効して
 * いる場合も 204 なので、ここに来るのは通信断のときだけ）。
 */
export function LogoutButton() {
	const queryClient = useQueryClient();
	const navigate = useNavigate();

	const mutation = useMutation({
		mutationFn: logout,
		onSettled: async () => {
			queryClient.setQueryData(authKeys.me(), null);
			// ログイン中に読んだ他のデータ（config 等）を次の利用者へ残さない。
			queryClient.clear();
			await navigate(LOGIN_PATH, { replace: true });
		}
	});

	return (
		<Button
			type="button"
			variant="ghost"
			size="sm"
			disabled={mutation.isPending}
			onClick={() => mutation.mutate()}
		>
			{mutation.isPending ? "ログアウト中…" : "ログアウト"}
		</Button>
	);
}
