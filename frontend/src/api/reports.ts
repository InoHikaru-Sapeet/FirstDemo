/**
 * レポート閲覧 API（バックエンド T-27・T-36 の `routers/reports.py`）。
 *
 * ⚠️ **T-31 が未着手なのでレスポンスの型は手書き zod**（`api/auth.ts` と同じ扱い）。
 * T-31 に着手したら `openapi-typescript` の生成物へ寄せること。形はバックエンドの
 * `ReportListResponse` / `ReportResponse` / `ArticlesResponse` と 1:1。
 *
 * ---
 *
 * ⚠️ **メール版 HTML と Web 版で「同じ号」を指していること。**
 *
 * `GET /reports/{period}/articles` はメール版と**同じ選別**（`select_articles()`）を
 * 通した結果を返す。フロントで並べ替えたり件数を絞ったりしないこと——そうすると
 * 「メールに載っていない記事が Web にある」状態になり、どちらが号の内容なのかが
 * 決まらなくなる。
 *
 * ⚠️ **示唆はメール版より多く返る**（メール版はセクション先頭1件だけ＝T-48 Step 1）。
 * 全件をトグルで開けるのが Web 版の役割。
 */

import { z } from "zod";
import { API_BASE_PATH, apiJson } from "@/api/client";

/** ジョブ種別（バックエンド `enterprise/entities/run_job.py` の `RunType`）。 */
export const runTypeSchema = z.enum(["weekly", "monthly"]);
export type RunType = z.infer<typeof runTypeSchema>;

/** `ReportListEntry`。`industries` は**出ている HTML のぶんだけ**（月刊は空）。 */
export const reportListEntrySchema = z.object({
	period: z.string(),
	type: runTypeSchema,
	industries: z.array(z.string())
});
export type ReportListEntry = z.infer<typeof reportListEntrySchema>;

export const reportListSchema = z.object({
	reports: z.array(reportListEntrySchema)
});

/** `ReportHtml`。`industry` は月刊で `null`（業界別ではない）。 */
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
	source: z.string()
});
export type ArticleCard = z.infer<typeof articleCardSchema>;

export const articleSectionSchema = z.object({
	heading: z.string(),
	articles: z.array(articleCardSchema)
});
export type ArticleSection = z.infer<typeof articleSectionSchema>;

export const articlesSchema = z.object({
	period: z.string(),
	industry: z.string(),
	industries: z.array(z.string()),
	point_of_week: z.string().nullable(),
	sections: z.array(articleSectionSchema)
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

/**
 * `GET /reports/{period}/articles`。**週刊のみ**（月刊は 404）。
 *
 * @param industry 省略するとその週の先頭の業界版
 */
export async function fetchArticles(
	period: string,
	industry?: string
): Promise<Articles> {
	const query =
		industry === undefined ? "" : `?industry=${encodeURIComponent(industry)}`;
	return apiJson(
		`/reports/${encodeURIComponent(period)}/articles${query}`,
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
