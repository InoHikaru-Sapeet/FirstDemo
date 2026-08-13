import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "@/App";
import {
	errorResponse,
	jsonResponse,
	noContentResponse,
	stubFetch
} from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

const ADMIN = {
	user_id: "usr_admin",
	email: "admin@sapeet.com",
	display_name: "運用担当",
	role: "admin"
};

const UNAUTHENTICATED_ME = () => errorResponse(401, "ログインが必要です。");

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("AppRoutes", () => {
	it("ログイン済みならナビ・利用者表示・ログアウト導線が出る", async () => {
		stubFetch({ "/api/auth/me": () => jsonResponse(ADMIN) });

		renderWithProviders(<AppRoutes />, { initialEntries: ["/"] });

		expect(
			await screen.findByRole("heading", { name: "レポート一覧" })
		).toBeInTheDocument();
		expect(screen.getByText("判断基準（管理者）")).toBeInTheDocument();
		expect(screen.getByText("運用担当（管理者）")).toBeInTheDocument();
		expect(
			screen.getByRole("button", { name: "ログアウト" })
		).toBeInTheDocument();
	});

	it("未ログインで認証必須ページを開くとログイン画面になる", async () => {
		stubFetch({ "/api/auth/me": UNAUTHENTICATED_ME });

		renderWithProviders(<AppRoutes />, { initialEntries: ["/admin/config"] });

		expect(
			await screen.findByRole("heading", { name: "ログイン" })
		).toBeInTheDocument();
		// config の存在・中身を示唆しない（実体はサーバー側の 403）。
		expect(screen.queryByText(/判断基準/)).not.toBeInTheDocument();
	});

	it("登録画面はログイン不要で開け、ログイン済み用のナビを出さない", async () => {
		stubFetch({ "/api/auth/me": UNAUTHENTICATED_ME });

		renderWithProviders(<AppRoutes />, { initialEntries: ["/register"] });

		expect(
			await screen.findByRole("heading", { name: "新規登録" })
		).toBeInTheDocument();
		expect(screen.queryByText("レポート一覧")).not.toBeInTheDocument();
		expect(
			screen.queryByRole("button", { name: "ログアウト" })
		).not.toBeInTheDocument();
	});

	it("ログアウトするとログイン画面へ戻る", async () => {
		let authenticated = true;
		stubFetch({
			"/api/auth/me": () =>
				authenticated ? jsonResponse(ADMIN) : UNAUTHENTICATED_ME(),
			"POST /api/auth/logout": () => {
				authenticated = false;
				return noContentResponse();
			}
		});

		renderWithProviders(<AppRoutes />, { initialEntries: ["/"] });

		fireEvent.click(await screen.findByRole("button", { name: "ログアウト" }));

		expect(
			await screen.findByRole("heading", { name: "ログイン" })
		).toBeInTheDocument();
	});
});
