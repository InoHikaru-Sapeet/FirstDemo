import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router";
import { queryClient } from "@/api/query-client";
import { AppLayout } from "@/components/common/AppLayout";
import { RequireAuth } from "@/components/common/RequireAuth";
import { AdminConfigPage } from "@/components/pages/AdminConfigPage";
import { LoginPage } from "@/components/pages/LoginPage";
import { RegisterPage } from "@/components/pages/RegisterPage";
import { ReportsPage } from "@/components/pages/ReportsPage";
import { LOGIN_PATH, REGISTER_PATH } from "@/utils/loginRedirect";

/**
 * ルート定義（T-43）。
 *
 * **ログイン不要なページ（`/login` / `/register`）と、認証が必要なページを
 * 明確に分ける。** 認証必須側は `RequireAuth` の下にまとめてあるので、
 * 画面を追加するときは `AppLayout` の子として足せば自動的に保護される
 * （T-32〜T-36 はこちら側）。
 *
 * ⚠️ ここでの保護は導線の整備であって、アクセス制御の実体はサーバー側
 * （未認証 401 / 権限なし 403。§6.1・§6.2）。
 */
export function AppRoutes() {
	return (
		<Routes>
			{/* ログイン不要 */}
			<Route path={LOGIN_PATH} element={<LoginPage />} />
			<Route path={REGISTER_PATH} element={<RegisterPage />} />

			{/* 認証が必要 */}
			<Route element={<RequireAuth />}>
				<Route element={<AppLayout />}>
					<Route path="/" element={<ReportsPage />} />
					{/* admin以外にはサーバ側でも403（設計書§5.1・§7.1） */}
					<Route path="/admin/config" element={<AdminConfigPage />} />
				</Route>
			</Route>
		</Routes>
	);
}

function App() {
	return (
		<QueryClientProvider client={queryClient}>
			<BrowserRouter>
				<AppRoutes />
			</BrowserRouter>
		</QueryClientProvider>
	);
}

export default App;
