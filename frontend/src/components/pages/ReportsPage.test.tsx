/**
 * レポート一覧・閲覧ページ（T-36）。重点:
 *
 * - **一覧 → 号の選択 → 詳細**（新しい号が既定で開く）
 * - **週刊は記事ごとのトグル開閉**で要約・示唆が出る（Web なので JS 可）
 * - ⚠️ **メール版が絞った示唆も Web では全件開ける**（T-48 Step 1 との対）
 * - ⚠️ **合計スコア・しきい値を画面に出さない**（サーバーも返さない）
 * - viewer に「閲覧のみ」の案内が出る（T-36 完了条件）
 */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
	READ_MORE_LABEL,
	ReportsPage,
	VIEWER_NOTICE
} from "@/components/pages/ReportsPage";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

const ADMIN = {
	user_id: "usr_admin",
	email: "admin@sapeet.com",
	display_name: "運用担当",
	role: "admin"
};

const VIEWER = { ...ADMIN, user_id: "usr_v", role: "viewer" };

const WEEKLY = "2026-W31";
const MONTHLY = "2026-07";

const LIST = {
	reports: [
		{ period: WEEKLY, type: "weekly", industries: ["不動産", "金融"] },
		{ period: MONTHLY, type: "monthly", industries: [] }
	]
};

const WEEKLY_REPORT = {
	period: WEEKLY,
	type: "weekly",
	html_urls: [
		{
			industry: "不動産",
			url: `/files/weekly_ai_intelligence_newsletter_不動産_${WEEKLY}.html`
		}
	],
	xlsx_url: `/files/weekly_ai_intelligence_report.xlsx#sheet=${WEEKLY}`,
	summary: { adopted: 11, excluded: 3 }
};

const MONTHLY_REPORT = {
	period: MONTHLY,
	type: "monthly",
	html_urls: [{ industry: null, url: `/files/monthly_belief_${MONTHLY}.html` }],
	xlsx_url: `/files/monthly_ai_leading_cases.xlsx#sheet=${MONTHLY}`,
	summary: { adopted: 4, excluded: 0 }
};

function card(index: number, overrides: Record<string, unknown> = {}) {
	return {
		category_id: "enterprise_ai_case",
		category_label: "企業AI活用事例",
		category_color: "#059669",
		title: `記事${index}`,
		url: `https://example.com/news/${index}`,
		summary: `要約${index}。`,
		insight: `示唆${index}。`,
		source: "ITmedia",
		...overrides
	};
}

const ARTICLES = {
	period: WEEKLY,
	industry: "不動産",
	industries: ["不動産", "金融"],
	point_of_week: "今週の総括。",
	sections: [
		{ heading: "不動産関連トピック", articles: [card(0), card(1)] },
		{ heading: "業界共通トピック", articles: [card(2)] }
	]
};

/** 週刊が既定で開く状態の一式（記事まで揃った形）。 */
function stubWeekly(overrides: Record<string, unknown> = {}) {
	return stubFetch({
		"/api/auth/me": () => jsonResponse(ADMIN),
		"/api/reports": () => jsonResponse(LIST),
		[`/api/reports/${WEEKLY}`]: () => jsonResponse(WEEKLY_REPORT),
		[`/api/reports/${MONTHLY}`]: () => jsonResponse(MONTHLY_REPORT),
		[`/api/reports/${WEEKLY}/articles`]: () =>
			jsonResponse({ ...ARTICLES, ...overrides })
	});
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("ReportsPage 一覧", () => {
	it("読めるレポートが無いときは空であることを伝える", async () => {
		stubFetch({
			"/api/auth/me": () => jsonResponse(ADMIN),
			"/api/reports": () => jsonResponse({ reports: [] })
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText(/まだレポートがありません/)).toBeVisible();
	});

	it("一覧を並べ、いちばん新しい号を既定で開く", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		const buttons = await screen.findAllByRole("button", {
			name: /2026-/
		});
		expect(buttons[0]).toHaveTextContent(WEEKLY);
		expect(buttons[0]).toHaveAttribute("aria-current", "true");
		// 詳細（採用/除外の件数サマリ）が既定で開いた号のもの。
		expect(await screen.findByText("採用 11 件 ／ 除外 3 件")).toBeVisible();
	});

	it("号を選ぶと詳細が切り替わる", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));

		expect(await screen.findByText("採用 4 件 ／ 除外 0 件")).toBeVisible();
	});

	it("一覧の取得に失敗したらサーバーの文言を出す", async () => {
		stubFetch({
			"/api/auth/me": () => jsonResponse(ADMIN),
			"/api/reports": () =>
				errorResponse(500, { message: "成果物置き場を読めません。" })
		});

		renderWithProviders(<ReportsPage />);

		// ⚠️ `createQueryClient()` は `retry: 1`（アプリと同じ設定）なので、
		// 失敗が確定するまで1回の再試行ぶん待つ必要がある。
		expect(
			await screen.findByText("成果物置き場を読めません。", undefined, {
				timeout: 3000
			})
		).toBeVisible();
	});

	it("viewer には「閲覧のみ」の案内を出す", async () => {
		stubFetch({
			"/api/auth/me": () => jsonResponse(VIEWER),
			"/api/reports": () => jsonResponse({ reports: [] })
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText(VIEWER_NOTICE)).toBeVisible();
	});

	it("admin には「閲覧のみ」の案内を出さない", async () => {
		stubFetch({
			"/api/auth/me": () => jsonResponse(ADMIN),
			"/api/reports": () => jsonResponse({ reports: [] })
		});

		renderWithProviders(<ReportsPage />);

		await screen.findByText(/まだレポートがありません/);
		expect(screen.queryByText(VIEWER_NOTICE)).not.toBeInTheDocument();
	});
});

describe("ReportsPage 週刊の記事トグル", () => {
	it("閉じているときは要約と示唆を出さない", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("記事0")).toBeVisible();
		expect(screen.queryByText("要約0。")).not.toBeInTheDocument();
		expect(screen.queryByText("示唆0。")).not.toBeInTheDocument();
		expect(
			screen.getAllByRole("button", { name: "要約と示唆を開く" })[0]
		).toHaveAttribute("aria-expanded", "false");
	});

	it("トグルを開くと要約と示唆が出る", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		const toggles = await screen.findAllByRole("button", {
			name: "要約と示唆を開く"
		});
		fireEvent.click(toggles[0]);

		expect(await screen.findByText("要約0。")).toBeVisible();
		expect(screen.getByText("示唆0。")).toBeVisible();
		expect(
			screen.getByRole("button", { name: "要約と示唆を閉じる" })
		).toHaveAttribute("aria-expanded", "true");
	});

	it("開閉は記事ごとに独立している", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		const toggles = await screen.findAllByRole("button", {
			name: "要約と示唆を開く"
		});
		fireEvent.click(toggles[1]);

		expect(await screen.findByText("要約1。")).toBeVisible();
		expect(screen.queryByText("要約0。")).not.toBeInTheDocument();
	});

	it("⚠️ メール版が絞った示唆も、開けばすべて読める（T-48 Step 1 との対）", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		const toggles = await screen.findAllByRole("button", {
			name: "要約と示唆を開く"
		});
		// メール版はセクション先頭の1件だけ示唆を出す。Web は3件すべて開ける。
		expect(toggles).toHaveLength(3);
		for (const toggle of toggles) {
			fireEvent.click(toggle);
		}

		for (const text of ["示唆0。", "示唆1。", "示唆2。"]) {
			expect(await screen.findByText(text)).toBeVisible();
		}
	});

	it("示唆が無い記事は要約だけを開く", async () => {
		stubWeekly({
			sections: [
				{
					heading: "不動産関連トピック",
					articles: [card(0, { insight: null })]
				}
			]
		});

		renderWithProviders(<ReportsPage />);

		fireEvent.click(
			await screen.findByRole("button", { name: "要約と示唆を開く" })
		);

		expect(await screen.findByText("要約0。")).toBeVisible();
		// ⚠️ `/示唆/` で探すとトグルのラベル（「要約と示唆を閉じる」）に当たる。
		// 示唆の本文が出ていないことを見る。
		expect(screen.queryByText("示唆0。")).not.toBeInTheDocument();
	});

	it("今週のポイントとセクション見出しを出す", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("今週の総括。")).toBeVisible();
		expect(
			screen.getByRole("heading", { name: "不動産関連トピック" })
		).toBeVisible();
		expect(
			screen.getByRole("heading", { name: "業界共通トピック" })
		).toBeVisible();
	});

	it("⚠️ 合計スコアやしきい値を画面に出さない", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		await screen.findByText("記事0");
		// 「採用 11 件 ／ 除外 3 件」以外に数値の指標を出していないこと。
		expect(screen.queryByText(/スコア/)).not.toBeInTheDocument();
		expect(screen.queryByText(/しきい値/)).not.toBeInTheDocument();
	});

	it("業界版を切り替えると、その版の記事を取り直す", async () => {
		const { requests } = stubFetch({
			"/api/auth/me": () => jsonResponse(ADMIN),
			"/api/reports": () => jsonResponse(LIST),
			[`/api/reports/${WEEKLY}`]: () => jsonResponse(WEEKLY_REPORT),
			[`/api/reports/${WEEKLY}/articles`]: () => jsonResponse(ARTICLES),
			[`/api/reports/${WEEKLY}/articles?industry=%E9%87%91%E8%9E%8D`]: () =>
				jsonResponse({
					...ARTICLES,
					industry: "金融",
					sections: [{ heading: "業界共通トピック", articles: [card(9)] }]
				})
		});

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: "金融 版" }));

		expect(await screen.findByText("記事9")).toBeVisible();
		await waitFor(() => {
			expect(
				requests.some((request) => request.url.includes("industry="))
			).toBe(true);
		});
	});

	it("記事の URL が使えない場合はリンクにしない", async () => {
		stubWeekly({
			sections: [
				{
					heading: "不動産関連トピック",
					articles: [card(0, { url: null, title: "リンク無しの記事" })]
				}
			]
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("リンク無しの記事")).toBeVisible();
		expect(
			screen.queryByRole("link", { name: "リンク無しの記事" })
		).not.toBeInTheDocument();
	});

	// --- 見出しとリンクの分離（T-50。メール版 `weekly_renderer` と対）-------

	it("⚠️ 記事タイトルはプレーン見出しでリンクにしない", async () => {
		stubWeekly({
			sections: [
				{
					heading: "不動産関連トピック",
					articles: [card(0, { title: "見出し" })]
				}
			]
		});

		renderWithProviders(<ReportsPage />);

		expect(
			await screen.findByRole("heading", { name: "見出し" })
		).toBeVisible();
		expect(
			screen.queryByRole("link", { name: "見出し" })
		).not.toBeInTheDocument();
	});

	it("記事へのリンクは出典行に置く（出典：〈ソース〉（記事を読む））", async () => {
		stubWeekly({
			sections: [
				{
					heading: "不動産関連トピック",
					articles: [
						card(0, {
							source: "日経クロステック",
							url: "https://example.com/42"
						})
					]
				}
			]
		});

		renderWithProviders(<ReportsPage />);

		const link = await screen.findByRole("link", { name: READ_MORE_LABEL });
		expect(link).toHaveAttribute("href", "https://example.com/42");
		expect(link.closest("p")?.textContent).toBe(
			`出典：日経クロステック（${READ_MORE_LABEL}）`
		);
	});

	it("URL が使えない記事では出典行の括弧ごと出さない", async () => {
		stubWeekly({
			sections: [
				{
					heading: "不動産関連トピック",
					articles: [card(0, { url: null, source: "個人ブログ" })]
				}
			]
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("出典：個人ブログ")).toBeVisible();
		expect(
			screen.queryByRole("link", { name: READ_MORE_LABEL })
		).not.toBeInTheDocument();
	});
});

describe("ReportsPage 生成物へのリンク", () => {
	it("メール版 HTML と中間xlsx へのリンクを `/api` 付きで出す", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		const html = await screen.findByRole("link", {
			name: "メール版 HTML を開く（不動産 版）"
		});
		expect(html).toHaveAttribute(
			"href",
			`/api${WEEKLY_REPORT.html_urls[0].url}`
		);
		expect(
			screen.getByRole("link", { name: "中間xlsx をダウンロード" })
		).toHaveAttribute("href", `/api${WEEKLY_REPORT.xlsx_url}`);
	});

	it("月刊はメール版 HTML をそのまま埋め込む（記事トグルは出さない）", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));

		const frame = await screen.findByTitle("月刊ビリーフ（メール版 HTML）");
		expect(frame).toHaveAttribute(
			"src",
			`/api${MONTHLY_REPORT.html_urls[0].url}`
		);
		expect(
			screen.queryByRole("button", { name: "要約と示唆を開く" })
		).not.toBeInTheDocument();
	});
});
