import { Link, Outlet } from "react-router";
import { LogoutButton } from "@/components/common/LogoutButton";
import { useCurrentUser } from "@/hooks/useCurrentUser";

/** ロールの日本語表示（仕様書 §2 アクター表）。 */
const ROLE_LABELS = {
	admin: "管理者",
	editor: "編集者",
	viewer: "閲覧者",
	system: "システム"
} as const;

/**
 * ログイン済みの画面の外枠（ナビ＋利用者表示＋ログアウト）。
 *
 * ナビの出し分け（「判断基準（管理者）」を admin のみに出す）は T-32 の担当。
 * ここでは T-43 の範囲として**ログアウト導線と現在のロール表示**までを置く。
 */
export function AppLayout() {
	const { user } = useCurrentUser();

	return (
		<div className="min-h-screen">
			<nav className="flex items-center gap-4 border-b p-4 text-sm">
				<Link to="/">レポート一覧</Link>
				{/* admin以外にはサーバ側でも403（設計書§5.1・§7.1）。
				    リンク自体を admin のみに出すのは T-32。 */}
				<Link to="/admin/config">判断基準（管理者）</Link>
				<div className="ml-auto flex items-center gap-3">
					{user !== null && (
						<span className="text-muted-foreground">
							{user.display_name}（{ROLE_LABELS[user.role]}）
						</span>
					)}
					<LogoutButton />
				</div>
			</nav>
			<Outlet />
		</div>
	);
}
