import { Link } from "react-router";
import { useCurrentUser } from "@/hooks/useCurrentUser";

/** 管理専用サブ画面のルート（設計判断D。週刊／月刊どちらのナビからも同じ URL）。 */
export const ADMIN_CONFIG_PATH = "/admin/config";

export const ADMIN_CONFIG_LABEL = "判断基準（管理者）";

/**
 * 「判断基準（管理者）」への導線（T-32）。
 *
 * ⚠️ **非表示は補助であって、アクセス制御の実体ではない。** リンクを隠しても
 * `/admin/config` を直接叩けるので、実体はサーバーの 403（`require_admin`）と、
 * それを受けた画面側で config の存在も中身も示唆しないこと（`AdminConfigPage`
 * の `FORBIDDEN_MESSAGE`）。ここで隠すのは「押せないボタンを見せない」ため。
 *
 * ⚠️ **読み込み中は出さない。** `GET /auth/me` の応答を待たずに出すと、
 * 非 admin にもリンクが一瞬見える。
 */
export function AdminNavLink() {
	const { user } = useCurrentUser();

	if (user?.role !== "admin") {
		return null;
	}

	return <Link to={ADMIN_CONFIG_PATH}>{ADMIN_CONFIG_LABEL}</Link>;
}
