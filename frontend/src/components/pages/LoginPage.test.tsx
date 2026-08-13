import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "@/components/pages/LoginPage";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

const VIEWER = {
	user_id: "usr_1",
	email: "viewer@sapeet.com",
	display_name: "閲覧者",
	role: "viewer"
};

/** バックエンド `usecases/auth.py` の `LOGIN_FAILED_MESSAGE` そのまま。 */
const LOGIN_FAILED_MESSAGE = "メールアドレスまたはパスワードが違います。";

const UNAUTHENTICATED_ME = () => errorResponse(401, "ログインが必要です。");

function renderLoginPage(state?: unknown) {
	return renderWithProviders(
		<Routes>
			<Route path="/login" element={<LoginPage />} />
			<Route path="/" element={<div>レポート一覧の画面</div>} />
			<Route path="/admin/config" element={<div>判断基準の画面</div>} />
		</Routes>,
		{ initialEntries: [{ pathname: "/login", state }] }
	);
}

function fillAndSubmit(email: string, password: string) {
	fireEvent.change(screen.getByLabelText("メールアドレス"), {
		target: { value: email }
	});
	fireEvent.change(screen.getByLabelText("パスワード"), {
		target: { value: password }
	});
	fireEvent.click(screen.getByRole("button", { name: "ログイン" }));
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("LoginPage", () => {
	it("パスワード欄はマスク表示で autocomplete が current-password", async () => {
		stubFetch({ "/api/auth/me": UNAUTHENTICATED_ME });

		renderLoginPage();

		const password = await screen.findByLabelText("パスワード");
		expect(password).toHaveAttribute("type", "password");
		expect(password).toHaveAttribute("autocomplete", "current-password");
		expect(screen.getByLabelText("メールアドレス")).toHaveAttribute(
			"autocomplete",
			"username"
		);
	});

	it("空欄のまま送信すると API を呼ばずに検証メッセージを出す", async () => {
		const { requests } = stubFetch({ "/api/auth/me": UNAUTHENTICATED_ME });

		renderLoginPage();
		fireEvent.click(await screen.findByRole("button", { name: "ログイン" }));

		expect(
			await screen.findByText("メールアドレスを入力してください。")
		).toBeInTheDocument();
		expect(
			screen.getByText("パスワードを入力してください。")
		).toBeInTheDocument();
		expect(requests.some((request) => request.url === "/api/auth/login")).toBe(
			false
		);
	});

	it("ログイン成功でログイン前に見ようとした URL へ戻る", async () => {
		let authenticated = false;
		stubFetch({
			"/api/auth/me": () =>
				authenticated ? jsonResponse(VIEWER) : UNAUTHENTICATED_ME(),
			"POST /api/auth/login": () => {
				authenticated = true;
				return jsonResponse({ status: "ok" });
			}
		});

		renderLoginPage({ from: "/admin/config" });
		await screen.findByLabelText("メールアドレス");
		fillAndSubmit("viewer@sapeet.com", "correct horse battery");

		expect(await screen.findByText("判断基準の画面")).toBeInTheDocument();
	});

	it("戻り先が無いときは既定の着地先（レポート一覧）へ行く", async () => {
		let authenticated = false;
		stubFetch({
			"/api/auth/me": () =>
				authenticated ? jsonResponse(VIEWER) : UNAUTHENTICATED_ME(),
			"POST /api/auth/login": () => {
				authenticated = true;
				return jsonResponse({ status: "ok" });
			}
		});

		renderLoginPage();
		await screen.findByLabelText("メールアドレス");
		fillAndSubmit("viewer@sapeet.com", "correct horse battery");

		expect(await screen.findByText("レポート一覧の画面")).toBeInTheDocument();
	});

	it("失敗時はサーバーの文言をそのまま出し、どちらが違うかを明かさない", async () => {
		stubFetch({
			"/api/auth/me": UNAUTHENTICATED_ME,
			"POST /api/auth/login": () =>
				errorResponse(401, {
					error: "invalid_credentials",
					message: LOGIN_FAILED_MESSAGE
				})
		});

		renderLoginPage();
		await screen.findByLabelText("メールアドレス");
		fillAndSubmit("unknown@sapeet.com", "wrong password 123");

		expect(await screen.findByRole("alert")).toHaveTextContent(
			LOGIN_FAILED_MESSAGE
		);
		// ⚠️ 「このメールは未登録です」等へ言い換えていないこと（アカウント列挙の防止）。
		expect(screen.queryByText(/未登録/)).not.toBeInTheDocument();
		expect(
			screen.queryByText(/アカウントが存在しません/)
		).not.toBeInTheDocument();
	});

	it("ログイン済みならログイン画面を出さずに着地先へ送る", async () => {
		stubFetch({ "/api/auth/me": () => jsonResponse(VIEWER) });

		renderLoginPage();

		expect(await screen.findByText("レポート一覧の画面")).toBeInTheDocument();
		await waitFor(() => {
			expect(
				screen.queryByRole("button", { name: "ログイン" })
			).not.toBeInTheDocument();
		});
	});
});
