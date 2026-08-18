/**
 * レポート閲覧 API（バックエンド T-27・T-36 の `routers/reports.py`）。
 *
 * ⚠️ **T-31 が未着手なのでレスポンスの型は手書き zod**（`api/auth.ts` と同じ扱い）。
 * T-31 に着手したら `openapi-typescript` の生成物へ寄せること。形はバックエンドの
 * `ReportListResponse` / `ReportResponse` / `ArticlesResponse` と 1:1。
 *
 * ---
 *
 * ⚠️ **生成 HTML と Web 版で「同じ号」を指していること。**
 *
 * `GET /reports/{period}/articles` は HTML と**同じ選別**（`select_articles()`）を
 * 通した結果を返す。フロントで並べ替えたり件数を絞ったりしないこと——そうすると
 * 「HTML に載っていない記事が Web にある」状態になり、どちらが号の内容なのかが
 * 決まらなくなる（**表示の件数を段階的に増やすのは可**＝T-52 Step 2 の「続きを見る」。
 * 順序と集合は変えない）。
 *
 * ⚠️ **示唆は HTML より多く返る**（HTML は先頭1件だけ＝T-48 Step 1）。
 * 全件をトグルで開けるのが Web 版の役割。
 *
 * ⚠️ **業界版は廃止**（T-52 Step 1）。`industry` / `industries` / `sections` は
 * 無くなり、記事は `articles` の1列で返る。
 */

import { z } from "zod";
import { API_BASE_PATH, apiJson } from "@/api/client";

/** ジョブ種別（バックエンド `enterprise/entities/run_job.py` の `RunType`）。 */
export const runTypeSchema = z.enum(["weekly", "monthly"]);
export type RunType = z.infer<typeof runTypeSchema>;

/** `ReportListEntry`。⚠️ `industries` は廃止（T-52 Step 1。業界版が無い）。 */
export const reportListEntrySchema = z.object({
	period: z.string(),
	type: runTypeSchema
});
export type ReportListEntry = z.infer<typeof reportListEntrySchema>;

export const reportListSchema = z.object({
	reports: z.array(reportListEntrySchema)
});

/** `ReportHtml`。⚠️ `industry` は**常に `null`**（T-52 Step 1 で業界版を廃止）。 */
export const reportHtmlSchema = z.object({
	industry: z.string().nullable(),
	url: z.string()
});
export type ReportHtml = z.infer<typeof reportHtmlSchema>;

export const reportSchema = z.object({
	period: z.string(),
	type: runTypeSchema,
	html_urls: z.array(reportHtmlSchema),
	xlsx_url: z.string(),
	summary: z.object({ adopted: z.number(), excluded: z.number() })
});
export type Report = z.infer<typeof reportSchema>;

/**
 * 図解（T-49。バックエンド `enterprise/entities/diagram.py` と 1:1）。
 *
 * ⚠️ **タイプは3種だけ**（`flow` / `compare` / `metrics`）。サーバーが返すのは
 * **構造化データ**で、描画済みの HTML ではない——描き方を決めるのは表示側
 * （メール版は `monthly_renderer`、Web 版は `ReportsPage` の `DiagramView`）。
 *
 * ⚠️ **週刊のメール版に図解は出ない**ので、週刊の図解が読めるのは Web だけ。
 */
export const diagramSchema = z.discriminatedUnion("type", [
	z.object({
		type: z.literal("flow"),
		title: z.string(),
		steps: z.array(z.string())
	}),
	z.object({
		type: z.literal("compare"),
		title: z.string(),
		left: z.object({ label: z.string(), points: z.array(z.string()) }),
		right: z.object({ label: z.string(), points: z.array(z.string()) })
	}),
	z.object({
		type: z.literal("metrics"),
		title: z.string(),
		items: z.array(z.object({ value: z.string(), label: z.string() }))
	})
]);
export type Diagram = z.infer<typeof diagramSchema>;

/**
 * `ArticleCard`。
 *
 * ⚠️ **合計スコアとしきい値は入っていない**（バックエンドが返さない。メール版
 * HTML に出ていない値を返すと config の値を推定できるため）。無いものを
 * フロントで補ってはいけない。
 */
export const articleCardSchema = z.object({
	category_id: z.string(),
	category_label: z.string(),
	category_color: z.string(),
	title: z.string(),
	url: z.string().nullable(),
	summary: z.string(),
	insight: z.string().nullable(),
	// ⚠️ 図解の無い記事は `null`（それが正常。無理に作らせていない）。
	diagram: diagramSchema.nullable(),
	source: z.string()
});
export type ArticleCard = z.infer<typeof articleCardSchema>;

/**
 * 今週のポイント1項目（T-52 Step 1）。
 *
 * 箇条書きの1行になる `heading` と、開いたときに出る `detail`。⚠️ **詳細が無い
 * 項目もある**（`null`）——そのときは開く口を出さない。
 */
export const pointOfWeekPointSchema = z.object({
	heading: z.string(),
	detail: z.string().nullable()
});
export type PointOfWeekPoint = z.infer<typeof pointOfWeekPointSchema>;

export const articlesSchema = z.object({
	period: z.string(),
	point_of_week: z.string().nullable(),
	point_of_week_points: z.array(pointOfWeekPointSchema),
	articles: z.array(articleCardSchema)
});
export type Articles = z.infer<typeof articlesSchema>;

/** `GET /reports`。読めるレポートの一覧（**新しい号が先**）。 */
export async function fetchReports(): Promise<ReportListEntry[]> {
	const body = await apiJson("/reports", reportListSchema);
	return body.reports;
}

/** `GET /reports/{period}`。生成物のリンクと件数サマリ。 */
export async function fetchReport(period: string): Promise<Report> {
	return apiJson(`/reports/${encodeURIComponent(period)}`, reportSchema);
}

/** `GET /reports/{period}/articles`。**週刊のみ**（月刊は 404）。 */
export async function fetchArticles(period: string): Promise<Articles> {
	return apiJson(
		`/reports/${encodeURIComponent(period)}/articles`,
		articlesSchema
	);
}

/**
 * 生成物（HTML / xlsx）を実際に取りに行く URL。
 *
 * ⚠️ API が返す `url` は `/files/...`（**プレフィックス無し**）。dev では Vite の
 * proxy が `/api` を落としてバックエンドへ渡すので、**ブラウザから開くときは
 * `/api` を足す**（`fetch` を通さない `<a href>` / `<iframe src>` も同じ）。
 */
export function artifactUrl(url: string): string {
	return `${API_BASE_PATH}${url}`;
}
