import { fireEvent, screen } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RegisterPage } from "@/components/pages/RegisterPage";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

const CREATED_VIEWER = {
	user_id: "usr_1",
	email: "newcomer@sapeet.com",
	display_name: "新入社員",
	role: "viewer"
};

const VALID_PASSWORD = "correct horse battery";

function renderRegisterPage() {
	return renderWithProviders(
		<Routes>
			<Route path="/register" element={<RegisterPage />} />
			<Route path="/login" element={<div>ログイン画面</div>} />
		</Routes>,
		{ initialEntries: ["/register"] }
	);
}

function fillForm(overrides: Partial<Record<string, string>> = {}) {
	const values: Record<string, string> = {
		メールアドレス: "newcomer@sapeet.com",
		表示名: "新入社員",
		パスワード: VALID_PASSWORD,
		"パスワード（確認）": VALID_PASSWORD,
		...overrides
	};

	for (const [label, value] of Object.entries(values)) {
		fireEvent.change(screen.getByLabelText(label), { target: { value } });
	}
	fireEvent.click(screen.getByRole("button", { name: "登録する" }));
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("RegisterPage", () => {
	it("ロール選択 UI を置かず、viewer になる旨を明示する", () => {
		stubFetch({});

		renderRegisterPage();

		expect(screen.getByText(/閲覧者（viewer）/)).toBeInTheDocument();
		expect(screen.getByText(/管理者へ依頼/)).toBeInTheDocument();
		// ロールを選ばせる UI が無いこと（自己登録でロールは決められない）。
		expect(screen.queryByLabelText(/権限/)).not.toBeInTheDocument();
		expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
		expect(screen.queryByRole("radio")).not.toBeInTheDocument();
	});

	it("パスワード欄はどちらもマスク表示で autocomplete が new-password", () => {
		stubFetch({});

		renderRegisterPage();

		for (const label of ["パスワード", "パスワード（確認）"]) {
			const field = screen.getByLabelText(label);
			expect(field).toHaveAttribute("type", "password");
			expect(field).toHaveAttribute("autocomplete", "new-password");
		}
	});

	it("送信するのは email / display_name / password だけ（role も確認欄も送らない）", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/register": () => jsonResponse(CREATED_VIEWER, 201)
		});

		renderRegisterPage();
		fillForm();

		expect(await screen.findByText("ログイン画面")).toBeInTheDocument();

		const registerRequest = requests.find(
			(request) => request.url === "/api/auth/register"
		);
		expect(registerRequest?.body).toEqual({
			email: "newcomer@sapeet.com",
			display_name: "新入社員",
			password: VALID_PASSWORD
		});
	});

	it("12文字未満のパスワードは API を呼ぶ前に弾く", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/register": () => jsonResponse(CREATED_VIEWER, 201)
		});

		renderRegisterPage();
		fillForm({ パスワード: "short", "パスワード（確認）": "short" });

		expect(
			await screen.findByText("パスワードは12文字以上にしてください。")
		).toBeInTheDocument();
		expect(
			requests.some((request) => request.url === "/api/auth/register")
		).toBe(false);
	});

	it("bcrypt の 72 バイト上限を文字数ではなくバイト長で見る（日本語24文字超）", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/register": () => jsonResponse(CREATED_VIEWER, 201)
		});
		// 25文字 = 75 バイト。文字数だけで見ていると通ってしまう。
		const tooLong = "あ".repeat(25);

		renderRegisterPage();
		fillForm({ パスワード: tooLong, "パスワード（確認）": tooLong });

		expect(await screen.findByText(/72 バイト以内/)).toBeInTheDocument();
		expect(
			requests.some((request) => request.url === "/api/auth/register")
		).toBe(false);
	});

	it("確認欄が一致しないと弾く", async () => {
		const { requests } = stubFetch({
			"POST /api/auth/register": () => jsonResponse(CREATED_VIEWER, 201)
		});

		renderRegisterPage();
		fillForm({ "パスワード（確認）": "different but long enough" });

		expect(
			await screen.findByText("パスワードが一致しません。")
		).toBeInTheDocument();
		expect(
			requests.some((request) => request.url === "/api/auth/register")
		).toBe(false);
	});

	it("登録済みメール（409）はサーバーの文言をそのまま出す", async () => {
		stubFetch({
			"POST /api/auth/register": () =>
				errorResponse(409, {
					error: "email_already_registered",
					message: "このメールアドレスは既に登録されています。"
				})
		});

		renderRegisterPage();
		fillForm();

		expect(await screen.findByRole("alert")).toHaveTextContent(
			"このメールアドレスは既に登録されています。"
		);
	});

	it("許可外ドメイン（422）もサーバーの文言をそのまま出す", async () => {
		stubFetch({
			"POST /api/auth/register": () =>
				errorResponse(422, {
					error: "email_domain_not_allowed",
					message: "登録できるのは次のドメインのみです：sapeet.com"
				})
		});

		renderRegisterPage();
		fillForm({ メールアドレス: "someone@example.com" });

		expect(await screen.findByRole("alert")).toHaveTextContent(
			"登録できるのは次のドメインのみです：sapeet.com"
		);
	});
});
