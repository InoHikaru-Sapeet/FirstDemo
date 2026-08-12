import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Link, Route, Routes } from "react-router";
import { queryClient } from "@/api/query-client";
import { AdminConfigPage } from "@/components/pages/AdminConfigPage";
import { ReportsPage } from "@/components/pages/ReportsPage";

function App() {
	return (
		<QueryClientProvider client={queryClient}>
			<BrowserRouter>
				<nav className="flex gap-4 border-b p-4 text-sm">
					<Link to="/">レポート一覧</Link>
					{/* admin以外にはサーバ側でも403（設計書§5.1・§7.1） */}
					<Link to="/admin/config">判断基準（管理者）</Link>
				</nav>
				<Routes>
					<Route path="/" element={<ReportsPage />} />
					<Route path="/admin/config" element={<AdminConfigPage />} />
				</Routes>
			</BrowserRouter>
		</QueryClientProvider>
	);
}

export default App;
