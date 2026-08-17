/**
 * 判断基準の編集画面（T-32・T-33・T-34・T-35 の最小実装）。重点:
 *
 * - ⚠️ **403 では config の項目名も既定値も出さない**（§6.1。T-32 完了条件）
 * - ⚠️ **403 でログイン画面へ飛ばさない**（401 との混同を防ぐ）
 * - 差分プレビュー（before → after）と、**変更ぶんだけの patch**
 * - **409** は「他の管理者が更新しました」、**422** は `issues[].path` を欄の下へ
 * - ドライランは**実ファイルを上書きしない／TTL で消える**旨を画面に出す
 */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
	AdminConfigPage,
	DRY_RUN_NOTICE,
	FORBIDDEN_MESSAGE
} from "@/components/pages/AdminConfigPage";
import { errorResponse, jsonResponse, stubFetch } from "@/test/http";
import { renderWithProviders } from "@/test/renderWithProviders";

const ADMIN = {
	user_id: "usr_admin",
	email: "admin@sapeet.com",
	display_name: "運用担当",
	role: "admin"
};

/** `GET /config` の返し（この画面が読む枝だけ）。 */
const CONFIG = {
	revision: 3,
	config: {
		tunable_thresholds: {
			min_total_score_to_publish: 55,
			min_reliability_score_to_publish: 5,
			adoption_class_score_map: {
				propose_next_meeting: 85,
				reference_info: 70,
				share_only: 60
			},
			weekly: {
				target_industries: ["不動産"],
				max_industry_topics: 6,
				max_common_topics: 8,
				point_of_week_required: true
			},
			monthly: {
				target_case_count: 15,
				chapter_count_hint: 5,
				min_score_for_case: 80,
				require_editorial_and_closing: true
			},
			dedup: {
				lookback_weeks: 4,
				monthly_lookback_months: 3,
				title_similarity_threshold: 0.85,
				treat_same_url_as_duplicate: true
			}
		},
		enums: { industry: ["不動産", "金融", "製造"] }
	}
};

function stubAdmin(routes: Record<string, () => Response> = {}) {
	return stubFetch({
		"/api/auth/me": () => jsonResponse(ADMIN),
		"/api/config": () => jsonResponse(CONFIG),
		...routes
	});
}

/** 掲載最低スコアを書き換える（数値欄の代表）。 */
function setPublishThreshold(value: string) {
	fireEvent.change(screen.getByLabelText("掲載最低スコア"), {
		target: { value }
	});
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("AdminConfigPage 到達と権限（T-32）", () => {
	it("⚠️ 403 のときは権限の文言だけを出し、config を示唆しない", async () => {
		stubFetch({
			"/api/auth/me": () => jsonResponse({ ...ADMIN, role: "editor" }),
			"/api/config": () =>
				errorResponse(403, { message: "この操作を行う権限がありません。" })
		});

		renderWithProviders(<AdminConfigPage />);

		expect(await screen.findByText(FORBIDDEN_MESSAGE)).toBeVisible();
		// config の項目名・既定値をいっさい出さない。
		expect(screen.queryByText(/掲載最低スコア/)).not.toBeInTheDocument();
		expect(screen.queryByText(/revision/)).not.toBeInTheDocument();
		expect(screen.queryByRole("button", { name: "保存する" })).toBeNull();
	});

	it("admin なら tunable の欄と現在の revision が出る", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);

		expect(await screen.findByLabelText("掲載最低スコア")).toHaveValue(55);
		expect(screen.getByText(/現在の revision/)).toHaveTextContent("3");
		expect(screen.getByLabelText("業界共通トピックの上限")).toHaveValue(8);
		expect(screen.getByLabelText("同一 URL を重複として扱う")).toBeChecked();
	});

	it("対象業界の選択肢は config の enums から作る", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);

		expect(await screen.findByLabelText("不動産")).toBeChecked();
		expect(screen.getByLabelText("金融")).not.toBeChecked();
		expect(screen.getByLabelText("製造")).not.toBeChecked();
	});
});

describe("AdminConfigPage 差分プレビュー（T-34）", () => {
	it("変更が無ければ保存できない", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);

		expect(await screen.findByText("変更はありません。")).toBeVisible();
		expect(screen.getByRole("button", { name: "保存する" })).toBeDisabled();
	});

	it("変更した項目を before → after で出す", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("62");

		const diff = screen.getByLabelText("変更内容");
		expect(diff).toHaveTextContent("掲載最低スコア：55 → 62");
		expect(screen.getByRole("button", { name: "保存する" })).toBeEnabled();
	});

	it("真偽値と対象業界も差分に出る", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);
		fireEvent.click(await screen.findByLabelText("金融"));
		fireEvent.click(screen.getByLabelText("同一 URL を重複として扱う"));

		const diff = screen.getByLabelText("変更内容");
		expect(diff).toHaveTextContent("対象業界：不動産 → 不動産・金融");
		expect(diff).toHaveTextContent("同一 URL を重複として扱う：有効 → 無効");
	});

	it("変更を破棄すると初期値へ戻る", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("62");
		fireEvent.click(screen.getByRole("button", { name: "変更を破棄する" }));

		expect(screen.getByLabelText("掲載最低スコア")).toHaveValue(55);
		expect(screen.getByText("変更はありません。")).toBeVisible();
	});
});

describe("AdminConfigPage 保存（T-34）", () => {
	it("⚠️ 変更ぶんだけを patch に入れ、base_revision を添えて送る", async () => {
		const { requests } = stubAdmin({
			"PUT /api/config": () =>
				jsonResponse({
					revision: 4,
					updated_at: "2026-08-17T09:00:00+09:00",
					updated_by: "usr_admin"
				})
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");
		fireEvent.click(screen.getByRole("button", { name: "保存する" }));

		await waitFor(() => {
			expect(requests.some((request) => request.method === "PUT")).toBe(true);
		});
		const put = requests.find((request) => request.method === "PUT");
		expect(put?.body).toEqual({
			base_revision: 3,
			// 触っていない項目は入らない（他の管理者の変更を巻き戻さないため）。
			patch: { tunable_thresholds: { min_total_score_to_publish: 50 } }
		});
	});

	it("成功したら新しい revision を伝える", async () => {
		let revision = 3;
		stubFetch({
			"/api/auth/me": () => jsonResponse(ADMIN),
			"/api/config": () => jsonResponse({ ...CONFIG, revision }),
			"PUT /api/config": () => {
				revision = 4;
				return jsonResponse({
					revision: 4,
					updated_at: "2026-08-17T09:00:00+09:00",
					updated_by: "usr_admin"
				});
			}
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");
		fireEvent.click(screen.getByRole("button", { name: "保存する" }));

		expect(await screen.findByText(/保存しました（revision 4）/)).toBeVisible();
		// 再取得でフォームが作り直され、新しい revision が初期値になる。
		await waitFor(() => {
			expect(screen.getByText(/現在の revision/)).toHaveTextContent("4");
		});
	});

	it("409 は「他の管理者が更新しました」と伝える", async () => {
		stubAdmin({
			"PUT /api/config": () =>
				errorResponse(409, {
					error: "revision_conflict",
					current_revision: 5
				})
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");
		fireEvent.click(screen.getByRole("button", { name: "保存する" }));

		expect(await screen.findByText(/他の管理者が更新しました/)).toBeVisible();
	});

	it("422 の issues[].path を該当欄の下へ出す", async () => {
		stubAdmin({
			"PUT /api/config": () =>
				errorResponse(422, {
					error: "validation_failed",
					issues: [
						{
							path: "tunable_thresholds.min_total_score_to_publish",
							reason: "share_only（60）以下にしてください。",
							code: "threshold_order_violation"
						}
					]
				})
		});

		renderWithProviders(<AdminConfigPage />);
		const input = await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("62");
		fireEvent.click(screen.getByRole("button", { name: "保存する" }));

		// ⚠️ **該当欄のところに出る**こと（画面上部の総括だけでは、どの欄を直せば
		// よいのか admin に伝わらない）。`role="alert"` は shadcn の `Alert` にも
		// 付くので、欄の周りを見て場所ごと固定する。
		await waitFor(() => {
			expect(input.parentElement).toHaveTextContent(
				"share_only（60）以下にしてください。"
			);
		});
	});

	it("⚠️ 422 を受けても入力値を自動補正しない（設計判断A）", async () => {
		stubAdmin({
			"PUT /api/config": () =>
				errorResponse(422, {
					error: "validation_failed",
					issues: [
						{
							path: "tunable_thresholds.min_total_score_to_publish",
							reason: "降順整合に違反します。",
							code: "threshold_order_violation"
						}
					]
				})
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("62");
		fireEvent.click(screen.getByRole("button", { name: "保存する" }));

		await screen.findByText("降順整合に違反します。");
		// 入力したままで、こちらが「通る値」へ書き換えたりしない。
		expect(screen.getByLabelText("掲載最低スコア")).toHaveValue(62);
		expect(screen.getByLabelText("変更内容")).toHaveTextContent(
			"掲載最低スコア：55 → 62"
		);
	});
});

describe("AdminConfigPage ドライラン（T-35）", () => {
	it("⚠️ 実ファイルを上書きしないこと・TTL で消えることを明示する", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);

		expect(await screen.findByText(DRY_RUN_NOTICE)).toBeVisible();
	});

	it("変更が無ければ実行できない", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);

		expect(
			await screen.findByRole("button", { name: "ドライランを実行" })
		).toBeDisabled();
	});

	it("期間が空のままでは実行できない", async () => {
		stubAdmin();

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");

		expect(
			screen.getByRole("button", { name: "ドライランを実行" })
		).toBeDisabled();
	});

	it("件数を before → after で即表示し、明細のリンクと TTL を出す", async () => {
		stubAdmin({
			"POST /api/config/dry-run": () =>
				jsonResponse({
					dry_run_id: "dry_abc",
					period: "2026-W31",
					base_revision: 3,
					scratch_url: "/config/dry-run/dry_abc/result.xlsx",
					summary: { adopted: 14, excluded: 41 },
					baseline: { adopted: 11, excluded: 44 },
					ttl_hours: 24
				})
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");
		fireEvent.change(screen.getByLabelText("対象期間"), {
			target: { value: "2026-W31" }
		});
		fireEvent.click(screen.getByRole("button", { name: "ドライランを実行" }));

		expect(await screen.findByText(/採用 11 →/)).toHaveTextContent(
			"採用 11 → 14 件 ／ 除外 44 → 41 件"
		);
		expect(
			screen.getByRole("link", { name: /明細（除外区分・理由つき）/ })
		).toHaveAttribute("href", "/api/config/dry-run/dry_abc/result.xlsx");
		expect(screen.getByText(/24 時間後に削除されます/)).toBeVisible();
	});

	it("⚠️ 422 not_previewable はサーバーの文言をそのまま出す", async () => {
		const message =
			"この変更は既存の採点結果へ再適用できないため試算できません（要再採点）。";
		stubAdmin({
			"POST /api/config/dry-run": () =>
				errorResponse(422, { error: "not_previewable", message })
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");
		fireEvent.change(screen.getByLabelText("対象期間"), {
			target: { value: "2026-W31" }
		});
		fireEvent.click(screen.getByRole("button", { name: "ドライランを実行" }));

		// 「件数に影響しません」等に言い換えない（実際には試算できないだけ）。
		expect(await screen.findByText(message)).toBeVisible();
	});

	it("ドライランは変更ぶんの patch と base_revision を送る", async () => {
		const { requests } = stubAdmin({
			"POST /api/config/dry-run": () =>
				jsonResponse({
					dry_run_id: "dry_abc",
					period: "2026-W31",
					base_revision: 3,
					scratch_url: "/config/dry-run/dry_abc/result.xlsx",
					summary: { adopted: 14, excluded: 41 },
					baseline: { adopted: 11, excluded: 44 },
					ttl_hours: 24
				})
		});

		renderWithProviders(<AdminConfigPage />);
		await screen.findByLabelText("掲載最低スコア");
		setPublishThreshold("50");
		fireEvent.change(screen.getByLabelText("対象期間"), {
			target: { value: "2026-W31" }
		});
		fireEvent.click(screen.getByRole("button", { name: "ドライランを実行" }));

		await waitFor(() => {
			expect(requests.some((request) => request.url.includes("dry-run"))).toBe(
				true
			);
		});
		const post = requests.find((request) => request.url.includes("dry-run"));
		expect(post?.body).toEqual({
			period: "2026-W31",
			candidate_config_patch: {
				tunable_thresholds: { min_total_score_to_publish: 50 }
			},
			base_revision: 3
		});
	});
});
