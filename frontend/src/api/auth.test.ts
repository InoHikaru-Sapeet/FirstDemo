import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchCurrentUser, login, logout, register } from "@/api/auth";
import { isForbidden } from "@/api/client";
import {
	errorResponse,
	jsonResponse,
	noContentResponse,
	stubFetch
} from "@/test/http";

const VIEWER = {
	user_id: "usr_1",
	email: "viewer@sapeet.com",
	display_name: "閲覧者",
	role: "viewer"
};

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("fetchCurrentUser", () => {
	it("ログイン中の利用者を返す", async () => {
		stubFetch({ "/api/auth/me": () => jsonResponse(VIEWER) });

		await expect(fetchCurrentUser()).resolves.toEqual(VIEWER);
	});

	it("401 は例外ではなく null（未ログインは正常な答え）", async () => {
		stubFetch({
			"/api/auth/me": () => errorResponse(401, "ログインが必要です。")
		});

		await expect(fetchCurrentUser()).resolves.toBeNull();
	});

	it("403 は null にせず投げる（401 と混同しない）", async () => {
		stubFetch({
			"/api/auth/me": () =>
				errorResponse(403, { message: "権限がありません。" })
		});

		const error = await fetchCurrentUser().catch((caught: unknown) => caught);

		expect(isForbidden(error)).toBe(true);
	});
});

describe("login / logout", () => {
	it("POST /auth/login にメールとパスワードだけを送る", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/login": () => jsonResponse({ status: "ok" })
		});

		await login({
			email: "viewer@sapeet.com",
			password: "correct horse battery"
		});

		expect(requests[0].method).toBe("POST");
		expect(requests[0].body).toEqual({
			email: "viewer@sapeet.com",
			password: "correct horse battery"
		});
	});

	it("POST /auth/logout は 204 を受け取れる", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/logout": () => noContentResponse()
		});

		await expect(logout()).resolves.toBeUndefined();
		expect(requests[0].url).toBe("/api/auth/logout");
	});
});

describe("register", () => {
	it("role を送らない（自己登録は必ず viewer）", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/register": () => jsonResponse(VIEWER, 201)
		});

		await register({
			email: "viewer@sapeet.com",
			display_name: "閲覧者",
			password: "correct horse battery"
		});

		expect(requests[0].body).toEqual({
			email: "viewer@sapeet.com",
			display_name: "閲覧者",
			password: "correct horse battery"
		});
		expect(Object.keys(Object(requests[0].body))).not.toContain("role");
	});

	it("作成されたユーザーを返す", async () => {
		stubFetch({ "POST /api/auth/register": () => jsonResponse(VIEWER, 201) });

		const user = await register({
			email: "viewer@sapeet.com",
			display_name: "閲覧者",
			password: "correct horse battery"
		});

		expect(user.role).toBe("viewer");
	});
});
