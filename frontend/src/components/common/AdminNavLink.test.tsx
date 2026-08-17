/**
 * 管理専用サブ画面への導線（T-32）。
 *
 * ⚠️ **非表示は補助**（アクセス制御の実体はサーバー 403）。ここで固定するのは
 * 「押せないリンクを見せない」ことと、**読み込み中に一瞬見えない**こと。
 */

import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
	ADMIN_CONFIG_LABEL,
	ADMIN_CONFIG_PATH,
	AdminNavLink
} from "@/components/common/AdminNavLink";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

const USER = {
	user_id: "usr_1",
	email: "someone@sapeet.com",
	display_name: "担当者",
	role: "admin"
};

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("AdminNavLink", () => {
	it("admin には管理画面へのリンクを出す", async () => {
		stubFetch({ "/api/auth/me": () => jsonResponse(USER) });

		renderWithProviders(<AdminNavLink />);

		expect(
			await screen.findByRole("link", { name: ADMIN_CONFIG_LABEL })
		).toHaveAttribute("href", ADMIN_CONFIG_PATH);
	});

	it.each(["editor", "viewer"])("%s には出さない", async (role) => {
		stubFetch({ "/api/auth/me": () => jsonResponse({ ...USER, role }) });

		const { queryClient } = renderWithProviders(<AdminNavLink />);
		await queryClient.getQueryCache().getAll()[0]?.fetch();

		expect(screen.queryByText(ADMIN_CONFIG_LABEL)).not.toBeInTheDocument();
	});

	it("未ログインでは出さない", async () => {
		stubFetch({
			"/api/auth/me": () => errorResponse(401, "ログインが必要です。")
		});

		const { queryClient } = renderWithProviders(<AdminNavLink />);
		await queryClient.getQueryCache().getAll()[0]?.fetch();

		expect(screen.queryByText(ADMIN_CONFIG_LABEL)).not.toBeInTheDocument();
	});

	it("⚠️ 読み込み中は出さない（非 admin に一瞬見えないこと）", () => {
		// `GET /auth/me` を解決させない（`isPending` のまま）。
		stubFetch({ "/api/auth/me": () => new Promise<Response>(() => {}) });

		renderWithProviders(<AdminNavLink />);

		expect(screen.queryByText(ADMIN_CONFIG_LABEL)).not.toBeInTheDocument();
	});
});
