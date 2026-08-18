/**
 * レポート一覧・閲覧ページ（T-36）。重点:
 *
 * - **一覧 → 号の選択 → 詳細**（新しい号が既定で開く）
 * - **週刊は記事ごとのトグル開閉**で要約・示唆が出る（Web なので JS 可）
 * - ⚠️ **メール版が絞った示唆も Web では全件開ける**（T-48 Step 1 との対）
 * - ⚠️ **図解は Web だけに出る**（週刊のメール版は描かない＝T-49）
 * - ⚠️ **合計スコア・しきい値を画面に出さない**（サーバーも返さない）
 * - viewer に「閲覧のみ」の案内が出る（T-36 完了条件）
 */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
	ALL_INDUSTRIES_LABEL,
	DIAGRAM_LABEL,
	INITIAL_ARTICLE_COUNT,
	POINT_OF_WEEK_HEADING,
	READ_MORE_LABEL,
	ReportsPage,
	showMoreLabel,
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
		{ period: WEEKLY, type: "weekly" },
		{ period: MONTHLY, type: "monthly" }
	]
};

const WEEKLY_REPORT = {
	period: WEEKLY,
	type: "weekly",
	// ⚠️ 週刊も1通・`industry` は `null`（T-52 Step 1 で業界版を廃止）。
	html_urls: [
		{
			industry: null,
			url: `/files/weekly_ai_intelligence_newsletter_${WEEKLY}.html`
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
		// ⚠️ 図解の無い記事が既定（T-49。無いのが正常な経路）。
		diagram: null,
		source: "ITmedia",
		...overrides
	};
}

const FLOW_DIAGRAM = {
	type: "flow",
	title: "契約業務の流れ",
	steps: ["契約書を受領", "AIが下書き", "担当者が確認"]
};

const COMPARE_DIAGRAM = {
	type: "compare",
	title: "導入前後の運用",
	left: { label: "従来", points: ["担当者が全文を読む"] },
	right: { label: "導入後", points: ["要点だけ確認する"] }
};

const METRICS_DIAGRAM = {
	type: "metrics",
	title: "導入の効果",
	items: [
		{ value: "月120時間", label: "削減した工数" },
		{ value: "-42%", label: "一次回答までの時間" }
	]
};

const ARTICLES = {
	period: WEEKLY,
	point_of_week: "今週の総括。",
	point_of_week_points: [
		{ heading: "実務投入が相次いだ。", detail: "詳細の段落。" },
		{ heading: "定型業務から広がっている。", detail: null }
	],
	// ⚠️ **点数順の1列**（T-52 Step 1。業界関連／業界共通の2セクションは廃止）。
	articles: [card(0), card(1), card(2)]
};

/** 月刊の事例1件（`GET /reports/{period}/cases` の形。T-52 Step 2）。 */
function monthlyCase(no: number, overrides: Record<string, unknown> = {}) {
	return {
		no,
		chapter: "第1章 業務自動化",
		organizations: ["A社"],
		title: `事例${no}`,
		url: `https://example.com/case/${no}`,
		source: "ITmedia（2026-07-27）",
		paragraphs: ["事実の段落。", "詳細の段落。", "示唆の段落。"],
		// ⚠️ 業界タグは月次8列に無い値（サーバーが narrative から解決したもの）。
		industries: ["不動産"],
		diagram: null,
		...overrides
	};
}

const CASES = {
	period: MONTHLY,
	editorial_subtitle: "問われ始めた月",
	editorial: "俯瞰の段落。",
	closing: "来月への視点。",
	industries: ["不動産", "金融"],
	cases: [monthlyCase(1), monthlyCase(2, { industries: ["金融"] })]
};

/** 月刊を開ける状態の一式（事例まで揃った形）。 */
function stubMonthly(overrides: Record<string, unknown> = {}) {
	return stubFetch({
		"/api/auth/me": () => jsonResponse(ADMIN),
		"/api/reports": () => jsonResponse(LIST),
		[`/api/reports/${WEEKLY}`]: () => jsonResponse(WEEKLY_REPORT),
		[`/api/reports/${MONTHLY}`]: () => jsonResponse(MONTHLY_REPORT),
		[`/api/reports/${WEEKLY}/articles`]: () => jsonResponse(ARTICLES),
		[`/api/reports/${MONTHLY}/cases`]: () =>
			jsonResponse({ ...CASES, ...overrides })
	});
}

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
			articles: [card(0, { insight: null })]
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

	it("今週のポイントと1つの見出しを出す", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		expect(
			await screen.findByRole("heading", { name: POINT_OF_WEEK_HEADING })
		).toBeVisible();
		// ⚠️ **セクションは1つ**（T-52 Step 1。業界関連／業界共通の廃止）。
		expect(
			screen.getByRole("heading", { name: "今週のトピック" })
		).toBeVisible();
		expect(screen.queryByText(/関連トピック/)).not.toBeInTheDocument();
		expect(screen.queryByText(/業界共通/)).not.toBeInTheDocument();
	});

	it("今週のポイントは箇条書きで、詳細は畳んでおく（T-52 Step 2）", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		const point = await screen.findByRole("button", {
			name: "・実務投入が相次いだ。"
		});
		expect(point).toHaveAttribute("aria-expanded", "false");
		expect(screen.queryByText("詳細の段落。")).not.toBeInTheDocument();
		// 連結した文章（メール版が描くもの）は箇条書きと二重に出さない。
		expect(screen.queryByText("今週の総括。")).not.toBeInTheDocument();
	});

	it("今週のポイントの項目をクリックすると詳細が開く", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(
			await screen.findByRole("button", { name: "・実務投入が相次いだ。" })
		);

		expect(await screen.findByText("詳細の段落。")).toBeVisible();
	});

	it("⚠️ 詳細が無い項目は開く口を出さない（開けるのに空を作らない）", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		expect(
			await screen.findByText("・定型業務から広がっている。")
		).toBeVisible();
		expect(
			screen.queryByRole("button", { name: "・定型業務から広がっている。" })
		).not.toBeInTheDocument();
	});

	it("項目が無い号では連結した今週のポイントをそのまま出す", async () => {
		stubWeekly({ point_of_week_points: [] });

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("今週の総括。")).toBeVisible();
	});

	it("記事は上位5件だけ出し、残りは「続きを見る」で開く（T-52 Step 2）", async () => {
		stubWeekly({
			articles: Array.from({ length: 8 }, (_, index) => card(index))
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("記事4")).toBeVisible();
		expect(screen.queryByText("記事5")).not.toBeInTheDocument();

		fireEvent.click(screen.getByRole("button", { name: showMoreLabel(3) }));

		expect(await screen.findByText("記事7")).toBeVisible();
		// 開いたら「続きを見る」は消える（押しても増えないボタンを残さない）。
		expect(
			screen.queryByRole("button", { name: /続きを見る/ })
		).not.toBeInTheDocument();
	});

	it("5件以下の号には「続きを見る」を出さない", async () => {
		stubWeekly({
			articles: Array.from({ length: INITIAL_ARTICLE_COUNT }, (_, i) => card(i))
		});

		renderWithProviders(<ReportsPage />);

		await screen.findByText("記事0");
		expect(
			screen.queryByRole("button", { name: /続きを見る/ })
		).not.toBeInTheDocument();
	});

	it("⚠️ 合計スコアやしきい値を画面に出さない", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		await screen.findByText("記事0");
		// 「採用 11 件 ／ 除外 3 件」以外に数値の指標を出していないこと。
		expect(screen.queryByText(/スコア/)).not.toBeInTheDocument();
		expect(screen.queryByText(/しきい値/)).not.toBeInTheDocument();
	});

	it("⚠️ 業界版の切り替えは無い（記事は業界を問わず1列）", async () => {
		const { requests } = stubFetch({
			"/api/auth/me": () => jsonResponse(ADMIN),
			"/api/reports": () => jsonResponse(LIST),
			[`/api/reports/${WEEKLY}`]: () => jsonResponse(WEEKLY_REPORT),
			[`/api/reports/${WEEKLY}/articles`]: () => jsonResponse(ARTICLES)
		});

		renderWithProviders(<ReportsPage />);

		await screen.findByText("記事0");
		// 業界チップも `industry` クエリも無い（T-52 Step 1）。
		expect(
			screen.queryByRole("button", { name: /版$/ })
		).not.toBeInTheDocument();
		await waitFor(() => {
			expect(
				requests.some((request) => request.url.includes("industry="))
			).toBe(false);
		});
	});

	it("記事の URL が使えない場合はリンクにしない", async () => {
		stubWeekly({
			articles: [card(0, { url: null, title: "リンク無しの記事" })]
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
			articles: [card(0, { title: "見出し" })]
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
			articles: [
				card(0, {
					source: "日経クロステック",
					url: "https://example.com/42"
				})
			]
		});

		renderWithProviders(<ReportsPage />);

		const link = await screen.findByRole("link", { name: READ_MORE_LABEL });
		expect(link).toHaveAttribute("href", "https://example.com/42");
		expect(link.closest("p")?.textContent).toBe(
			`出典：日経クロステック（${READ_MORE_LABEL}）`
		);
	});

	// --- 図解（T-49。**メール版に出ないので Web だけの表示**）--------------

	it("図解はトグルを開いたときだけ出る", async () => {
		stubWeekly({
			articles: [card(0, { diagram: FLOW_DIAGRAM })]
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("記事0")).toBeVisible();
		expect(screen.queryByText(FLOW_DIAGRAM.title)).not.toBeInTheDocument();

		fireEvent.click(screen.getByRole("button", { name: "要約と示唆を開く" }));

		expect(await screen.findByText(FLOW_DIAGRAM.title)).toBeVisible();
		expect(screen.getByText(DIAGRAM_LABEL)).toBeVisible();
	});

	it("flow は3〜5ステップを順に出す", async () => {
		stubWeekly({
			articles: [card(0, { diagram: FLOW_DIAGRAM })]
		});

		renderWithProviders(<ReportsPage />);
		fireEvent.click(
			await screen.findByRole("button", { name: "要約と示唆を開く" })
		);

		const steps =
			await screen.findAllByText(/契約書を受領|AIが下書き|担当者が確認/);
		expect(steps.map((node) => node.textContent)).toEqual(FLOW_DIAGRAM.steps);
	});

	it("compare は左右の見出しと要点を出す", async () => {
		stubWeekly({
			articles: [card(0, { diagram: COMPARE_DIAGRAM })]
		});

		renderWithProviders(<ReportsPage />);
		fireEvent.click(
			await screen.findByRole("button", { name: "要約と示唆を開く" })
		);

		expect(await screen.findByText("従来")).toBeVisible();
		expect(screen.getByText("導入後")).toBeVisible();
		expect(screen.getByText("担当者が全文を読む")).toBeVisible();
		expect(screen.getByText("要点だけ確認する")).toBeVisible();
	});

	it("metrics は値とラベルを対で出す", async () => {
		stubWeekly({
			articles: [card(0, { diagram: METRICS_DIAGRAM })]
		});

		renderWithProviders(<ReportsPage />);
		fireEvent.click(
			await screen.findByRole("button", { name: "要約と示唆を開く" })
		);

		for (const item of METRICS_DIAGRAM.items) {
			expect(await screen.findByText(item.value)).toBeVisible();
			expect(screen.getByText(item.label)).toBeVisible();
		}
	});

	it("⚠️ 図解が無い記事には図解の枠を出さない", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);
		const toggles = await screen.findAllByRole("button", {
			name: "要約と示唆を開く"
		});
		fireEvent.click(toggles[0]);

		expect(await screen.findByText("要約0。")).toBeVisible();
		expect(screen.queryByText(DIAGRAM_LABEL)).not.toBeInTheDocument();
	});

	it("要約も示唆も無くても、図解があればトグルを出す", async () => {
		stubWeekly({
			articles: [card(0, { summary: "", insight: null, diagram: FLOW_DIAGRAM })]
		});

		renderWithProviders(<ReportsPage />);
		fireEvent.click(
			await screen.findByRole("button", { name: "要約と示唆を開く" })
		);

		expect(await screen.findByText(FLOW_DIAGRAM.title)).toBeVisible();
	});

	it("URL が使えない記事では出典行の括弧ごと出さない", async () => {
		stubWeekly({
			articles: [card(0, { url: null, source: "個人ブログ" })]
		});

		renderWithProviders(<ReportsPage />);

		expect(await screen.findByText("出典：個人ブログ")).toBeVisible();
		expect(
			screen.queryByRole("link", { name: READ_MORE_LABEL })
		).not.toBeInTheDocument();
	});
});

describe("ReportsPage 生成物へのリンク", () => {
	it("⚠️ 「メール版 HTML を開く」リンクは出さない（T-52 Step 2）", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		await screen.findByText("記事0");
		// 生成 HTML は `GET /files` から従来どおり取れる（**廃止したのは導線だけ**）。
		expect(screen.queryByText(/メール版/)).not.toBeInTheDocument();
	});

	it("中間xlsx へのリンクは `/api` 付きで残す", async () => {
		stubWeekly();

		renderWithProviders(<ReportsPage />);

		expect(
			await screen.findByRole("link", { name: "中間xlsx をダウンロード" })
		).toHaveAttribute("href", `/api${WEEKLY_REPORT.xlsx_url}`);
	});
});

describe("ReportsPage 月刊の事例（T-52 Step 2）", () => {
	it("事例カードに業界タグを出す", async () => {
		stubMonthly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));

		expect(await screen.findByText("事例1")).toBeVisible();
		expect(screen.getByText("CASE 01")).toBeVisible();
		// 業界タグ（月次8列には無い値。narrative から来る）。
		expect(screen.getAllByText("不動産").length).toBeGreaterThan(0);
	});

	it("業界チップで絞り込む（表示だけ・順序は変えない）", async () => {
		stubMonthly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));
		await screen.findByText("事例1");

		fireEvent.click(screen.getByRole("button", { name: "金融" }));

		expect(await screen.findByText("事例2")).toBeVisible();
		expect(screen.queryByText("事例1")).not.toBeInTheDocument();

		// 「すべての業界」で絞り込みを外せる。
		fireEvent.click(screen.getByRole("button", { name: ALL_INDUSTRIES_LABEL }));
		expect(await screen.findByText("事例1")).toBeVisible();
	});

	it("⚠️ 候補が無い号ではチップを出さない", async () => {
		stubMonthly({
			industries: [],
			cases: [monthlyCase(1, { industries: [] })]
		});

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));

		expect(await screen.findByText("事例1")).toBeVisible();
		expect(
			screen.queryByRole("button", { name: ALL_INDUSTRIES_LABEL })
		).not.toBeInTheDocument();
	});

	it("巻頭言とむすびを出す", async () => {
		stubMonthly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));

		expect(await screen.findByText("俯瞰の段落。")).toBeVisible();
		expect(screen.getByText("来月への視点。")).toBeVisible();
	});

	it("⚠️ メール版 HTML の iframe は出さない（Web 版が唯一の閲覧形式）", async () => {
		stubMonthly();

		renderWithProviders(<ReportsPage />);

		fireEvent.click(await screen.findByRole("button", { name: /2026-07/ }));

		await screen.findByText("事例1");
		expect(
			screen.queryByTitle("月刊ビリーフ（メール版 HTML）")
		).not.toBeInTheDocument();
	});
});
