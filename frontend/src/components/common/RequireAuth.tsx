import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { buildLoginRedirectState, LOGIN_PATH } from "@/utils/loginRedirect";

type RequireAuthProps = {
	/** 省略時は `<Outlet />`（レイアウトルートとして使う場合）。 */
	children?: ReactNode;
};

/**
 * 認証が必要なページを囲む（T-43）。
 *
 * 未ログインならログイン画面へ送り、**元の URL を state で持たせて後で戻す**。
 *
 * ⚠️ これは導線の整備であって**アクセス制御ではない**。config を非 admin に
 * 見せない実体はサーバー側の 403（§6.1・T-09）。ここを外しても API は守られる、
 * という状態を保つこと。
 */
export function RequireAuth({ children }: RequireAuthProps) {
	const { isAuthenticated, isLoading } = useCurrentUser();
	const location = useLocation();

	if (isLoading) {
		// ⚠️ 読み込み中にリダイレクトしないこと。`GET /auth/me` の応答を待たずに
		// 判断すると、再読込のたびにログイン画面が一瞬見えてしまう。
		return (
			<div className="p-6 text-sm text-muted-foreground" role="status">
				読み込み中…
			</div>
		);
	}

	if (!isAuthenticated) {
		return (
			<Navigate
				to={LOGIN_PATH}
				state={buildLoginRedirectState(location)}
				replace
			/>
		);
	}

	return <>{children ?? <Outlet />}</>;
}
