import { screen } from "@testing-library/react";
import { Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireAuth } from "@/components/common/RequireAuth";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

/** 応答を握って「読み込み中」の状態を観測するための小道具。 */
function createDeferred() {
	let release: () => void = () => {};
	const promise = new Promise<void>((resolve) => {
		release = resolve;
	});
	return {
		promise,
		resolve: () => {
			release();
		}
	};
}

const ADMIN = {
	user_id: "usr_admin",
	email: "admin@sapeet.com",
	display_name: "管理者",
	role: "admin"
};

/** リダイレクト先で「どこから来たか」を確認するためのダミー画面。 */
function LoginProbe() {
	const location = useLocation();
	return (
		<div>
			<span>ログイン画面</span>
			<span data-testid="from">{JSON.stringify(location.state)}</span>
		</div>
	);
}

function renderProtected(initialPath: string) {
	return renderWithProviders(
		<Routes>
			<Route path="/login" element={<LoginProbe />} />
			<Route element={<RequireAuth />}>
				<Route path="/admin/config" element={<div>判断基準の画面</div>} />
			</Route>
		</Routes>,
		{ initialEntries: [initialPath] }
	);
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("RequireAuth", () => {
	it("ログイン済みなら中身を描画する", async () => {
		stubFetch({ "/api/auth/me": () => jsonResponse(ADMIN) });

		renderProtected("/admin/config");

		expect(await screen.findByText("判断基準の画面")).toBeInTheDocument();
	});

	it("未ログイン（401）ならログイン画面へ送り、元の URL を state で持たせる", async () => {
		stubFetch({
			"/api/auth/me": () => errorResponse(401, "ログインが必要です。")
		});

		renderProtected("/admin/config?tab=axes");

		expect(await screen.findByText("ログイン画面")).toBeInTheDocument();
		expect(screen.getByTestId("from")).toHaveTextContent(
			'{"from":"/admin/config?tab=axes"}'
		);
	});

	it("GET /auth/me の応答を待つ間はリダイレクトしない（ログイン画面が一瞬見えるのを防ぐ）", async () => {
		const pending = createDeferred();
		stubFetch({
			"/api/auth/me": async () => {
				await pending.promise;
				return jsonResponse(ADMIN);
			}
		});

		renderProtected("/admin/config");

		expect(await screen.findByRole("status")).toHaveTextContent("読み込み中");
		expect(screen.queryByText("ログイン画面")).not.toBeInTheDocument();

		pending.resolve();

		expect(await screen.findByText("判断基準の画面")).toBeInTheDocument();
	});
});
