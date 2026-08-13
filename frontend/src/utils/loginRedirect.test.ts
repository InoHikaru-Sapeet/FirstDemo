import { describe, expect, it } from "vitest";
import {
	buildLoginRedirectState,
	DEFAULT_AUTHENTICATED_PATH,
	readLoginRedirectTarget
} from "@/utils/loginRedirect";

describe("buildLoginRedirectState", () => {
	it("パスとクエリを繋げて持たせる", () => {
		expect(
			buildLoginRedirectState({
				pathname: "/admin/config",
				search: "?tab=axes"
			})
		).toEqual({ from: "/admin/config?tab=axes" });
	});
});

describe("readLoginRedirectTarget", () => {
	it("アプリ内のパスはそのまま返す", () => {
		expect(readLoginRedirectTarget({ from: "/admin/config" })).toBe(
			"/admin/config"
		);
	});

	it("state が無い・壊れているときは既定の着地先", () => {
		expect(readLoginRedirectTarget(null)).toBe(DEFAULT_AUTHENTICATED_PATH);
		expect(readLoginRedirectTarget(undefined)).toBe(DEFAULT_AUTHENTICATED_PATH);
		expect(readLoginRedirectTarget({ from: 42 })).toBe(
			DEFAULT_AUTHENTICATED_PATH
		);
		expect(readLoginRedirectTarget("/admin/config")).toBe(
			DEFAULT_AUTHENTICATED_PATH
		);
	});

	it("外部への遷移を弾く（オープンリダイレクトにしない）", () => {
		expect(readLoginRedirectTarget({ from: "//evil.example/steal" })).toBe(
			DEFAULT_AUTHENTICATED_PATH
		);
		expect(readLoginRedirectTarget({ from: "https://evil.example" })).toBe(
			DEFAULT_AUTHENTICATED_PATH
		);
	});

	it("ログイン・登録画面自身へは戻さない（往復になる）", () => {
		expect(readLoginRedirectTarget({ from: "/login" })).toBe(
			DEFAULT_AUTHENTICATED_PATH
		);
		expect(readLoginRedirectTarget({ from: "/register" })).toBe(
			DEFAULT_AUTHENTICATED_PATH
		);
	});
});
