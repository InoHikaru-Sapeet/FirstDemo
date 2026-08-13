import { afterEach, describe, expect, it, vi } from "vitest";
import {
	ApiError,
	apiJson,
	apiSend,
	isConflict,
	isForbidden,
	isUnauthorized,
	isValidationError,
	NETWORK_ERROR_STATUS
} from "@/api/client";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";

const passthrough = {
	parse: (data: unknown): unknown => data
};

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("apiClient", () => {
	it("すべてのリクエストに credentials: include を付ける（Cookie 認証の前提）", async () => {
		const { requests } = stubFetch({
			"/api/auth/me": () => jsonResponse({ ok: true })
		});

		await apiJson("/auth/me", passthrough);

		expect(requests).toHaveLength(1);
		expect(requests[0].credentials).toBe("include");
	});

	it("パスに /api を前置する（dev proxy 経由で同一オリジンにするため）", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/login": () => jsonResponse({ status: "ok" })
		});

		await apiSend("/auth/login", { method: "POST", body: { email: "a@b.co" } });

		expect(requests[0].url).toBe("/api/auth/login");
		expect(requests[0].body).toEqual({ email: "a@b.co" });
	});

	it("401 と 403 を混同しない", async () => {
		stubFetch({
			"/api/unauthorized": () =>
				errorResponse(401, {
					error: "unauthenticated",
					message: "ログインが必要です。"
				}),
			"/api/forbidden": () =>
				errorResponse(403, {
					error: "forbidden",
					message: "権限がありません。"
				})
		});

		const unauthorized = await apiJson("/unauthorized", passthrough).catch(
			(error: unknown) => error
		);
		const forbidden = await apiJson("/forbidden", passthrough).catch(
			(error: unknown) => error
		);

		expect(isUnauthorized(unauthorized)).toBe(true);
		expect(isForbidden(unauthorized)).toBe(false);
		expect(isForbidden(forbidden)).toBe(true);
		expect(isUnauthorized(forbidden)).toBe(false);
	});

	it("サーバーの文言をそのまま message に載せる（フロントで言い換えない）", async () => {
		stubFetch({
			"POST /api/auth/login": () =>
				errorResponse(401, {
					error: "invalid_credentials",
					message: "メールアドレスまたはパスワードが違います。"
				})
		});

		const error = await apiSend("/auth/login", {
			method: "POST",
			body: {}
		}).catch((caught: unknown) => caught);

		expect(error).toBeInstanceOf(ApiError);
		if (!(error instanceof ApiError)) {
			return;
		}
		expect(error.message).toBe("メールアドレスまたはパスワードが違います。");
		expect(error.code).toBe("invalid_credentials");
	});

	it("409（重複）を区別できる", async () => {
		stubFetch({
			"POST /api/auth/register": () =>
				errorResponse(409, {
					error: "email_already_registered",
					message: "このメールアドレスは既に登録されています。"
				})
		});

		const error = await apiJson("/auth/register", passthrough, {
			method: "POST",
			body: {}
		}).catch((caught: unknown) => caught);

		expect(isConflict(error)).toBe(true);
	});

	it("422 の issues[] を取り出す（パスワードポリシー違反）", async () => {
		stubFetch({
			"POST /api/auth/register": () =>
				errorResponse(422, {
					error: "validation_failed",
					issues: [
						{
							code: "password_too_short",
							reason: "パスワードは12文字以上にしてください（現在 4 文字）。"
						}
					]
				})
		});

		const error = await apiJson("/auth/register", passthrough, {
			method: "POST",
			body: {}
		}).catch((caught: unknown) => caught);

		expect(isValidationError(error)).toBe(true);
		if (!(error instanceof ApiError)) {
			return;
		}
		expect(error.issues).toHaveLength(1);
		expect(error.issues[0].code).toBe("password_too_short");
		// message が無い形でも issues の reason から文言を組み立てる。
		expect(error.message).toContain("12文字以上");
	});

	it("FastAPI の入力検証形式（detail が配列）からも文言を取れる", async () => {
		stubFetch({
			"POST /api/auth/register": () =>
				errorResponse(422, [
					{ loc: ["body", "role"], msg: "Extra inputs are not permitted" }
				])
		});

		const error = await apiJson("/auth/register", passthrough, {
			method: "POST",
			body: { role: "admin" }
		}).catch((caught: unknown) => caught);

		if (!(error instanceof ApiError)) {
			throw new Error("ApiError が投げられていない");
		}
		expect(error.message).toContain("Extra inputs are not permitted");
	});

	it("通信できないときは status 0 の ApiError にする", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(() => Promise.reject(new TypeError("Failed to fetch")))
		);

		const error = await apiJson("/auth/me", passthrough).catch(
			(caught: unknown) => caught
		);

		if (!(error instanceof ApiError)) {
			throw new Error("ApiError が投げられていない");
		}
		expect(error.status).toBe(NETWORK_ERROR_STATUS);
		expect(isUnauthorized(error)).toBe(false);
	});
});
