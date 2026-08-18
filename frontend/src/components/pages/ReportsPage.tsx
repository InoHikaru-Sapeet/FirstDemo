/**
 * レポート一覧・閲覧ページ（T-36。設計書 §3.3 ／ 仕様書 §6.2）。
 *
 * **認証済みの全ロールが閲覧できる**（config には一切触れない）。自己登録直後の
 * viewer が最初に着地する画面がここ（T-43）。
 *
 * ---
 *
 * ⚠️ **メール版 HTML と Web 版は「同じ号の別の見せ方」**。
 *
 * 配信物はメール版 HTML（`GET /files/...`）で、§7.1 で JS が禁止なので**要約を
 * 全角60字で切り、示唆をセクション先頭1件に絞っている**（T-48 Step 1）。この画面は
 * Web なので JS が使えるため、**同じ号の全文をトグルで開ける**——記事ごとに要約
 * （切っていない全文）と示唆（メール版が出していない分も含めて全件）を出す。
 *
 * ⚠️ **選別（採否・並び順・上限）はサーバーが返したまま使う。** 並べ替えや件数の
 * 絞り込みをこちらでやると「メールに載っていない記事が Web にある」状態になり、
 * どちらが号の内容なのか決まらなくなる（`api/reports.ts` の警告と対）。
 *
 * ⚠️ **メール版 HTML そのものも見られるようにしてある**（iframe と直リンク）。
 * 配信されるものを確認する経路が無いと、編集担当が「実際に届く形」を見られない。
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { toDisplayMessage } from "@/api/client";
import { reportKeys } from "@/api/query-keys";
import {
	type ArticleCard as ArticleCardData,
	artifactUrl,
	type Diagram,
	fetchArticles,
	fetchReport,
	fetchReports,
	type ReportListEntry
} from "@/api/reports";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/useCurrentUser";

const TYPE_LABELS = {
	weekly: "週刊メルマガ",
	monthly: "月刊ビリーフ"
} as const;

/** viewer に「見るだけの権限である」ことを伝える文（T-36 完了条件）。 */
export const VIEWER_NOTICE =
	"閲覧のみ可能な権限です。レポートの実行や判断基準の変更が必要な場合は管理者に依頼してください。";

export function ReportsPage() {
	const { user } = useCurrentUser();
	const [selected, setSelected] = useState<string | null>(null);

	const list = useQuery({
		queryKey: reportKeys.list(),
		queryFn: fetchReports
	});

	// ⚠️ **一覧が届いてから既定を決める**（`useEffect` で state を後追いさせない）。
	// 選択が未設定なら先頭＝いちばん新しい号を見ているものとして扱う。
	const reports = list.data ?? [];
	const period = selected ?? reports[0]?.period ?? null;

	return (
		<main className="mx-auto max-w-5xl p-6">
			<h1 className="text-xl font-semibold">レポート一覧</h1>
			<p className="mt-1 text-sm text-muted-foreground">
				週刊メルマガ・月刊ビリーフの生成結果を表示します。
			</p>

			{user?.role === "viewer" && (
				<Alert className="mt-4">
					<AlertDescription>{VIEWER_NOTICE}</AlertDescription>
				</Alert>
			)}

			{list.isPending && <p className="mt-6 text-sm">読み込み中…</p>}

			{list.isError && (
				<Alert variant="destructive" className="mt-6">
					<AlertDescription>{toDisplayMessage(list.error)}</AlertDescription>
				</Alert>
			)}

			{list.isSuccess && reports.length === 0 && (
				<p className="mt-6 text-sm text-muted-foreground">
					まだレポートがありません。パイプラインの実行が終わるとここに並びます。
				</p>
			)}

			{reports.length > 0 && period !== null && (
				<div className="mt-6 grid gap-6 md:grid-cols-[14rem_1fr]">
					<PeriodList
						reports={reports}
						selected={period}
						onSelect={setSelected}
					/>
					<ReportDetail key={period} period={period} />
				</div>
			)}
		</main>
	);
}

type PeriodListProps = {
	reports: ReportListEntry[];
	selected: string;
	onSelect: (period: string) => void;
};

function PeriodList({ reports, selected, onSelect }: PeriodListProps) {
	return (
		<nav aria-label="号の一覧">
			<ul className="space-y-1">
				{reports.map((report) => (
					<li key={report.period}>
						<button
							type="button"
							onClick={() => onSelect(report.period)}
							aria-current={report.period === selected}
							className={`w-full rounded border p-2 text-left text-sm ${
								report.period === selected ? "bg-muted font-semibold" : ""
							}`}
						>
							<span className="block">{report.period}</span>
							<span className="block text-xs text-muted-foreground">
								{TYPE_LABELS[report.type]}
								{report.industries.length > 0 &&
									`（${report.industries.join("・")}）`}
							</span>
						</button>
					</li>
				))}
			</ul>
		</nav>
	);
}

function ReportDetail({ period }: { period: string }) {
	const report = useQuery({
		queryKey: reportKeys.detail(period),
		queryFn: () => fetchReport(period)
	});

	if (report.isPending) {
		return <p className="text-sm">読み込み中…</p>;
	}
	if (report.isError) {
		return (
			<Alert variant="destructive">
				<AlertDescription>{toDisplayMessage(report.error)}</AlertDescription>
			</Alert>
		);
	}

	const data = report.data;
	return (
		<section aria-label={`${period} のレポート`}>
			<h2 className="text-lg font-semibold">
				{period}（{TYPE_LABELS[data.type]}）
			</h2>
			<p className="mt-1 text-sm text-muted-foreground">
				採用 {data.summary.adopted} 件 ／ 除外 {data.summary.excluded} 件
			</p>

			<ul className="mt-3 space-y-1 text-sm">
				{data.html_urls.map((html) => (
					<li key={html.url}>
						<a
							className="underline"
							href={artifactUrl(html.url)}
							target="_blank"
							rel="noreferrer"
						>
							{html.industry === null
								? "メール版 HTML を開く"
								: `メール版 HTML を開く（${html.industry} 版）`}
						</a>
					</li>
				))}
				<li>
					<a
						className="underline"
						href={artifactUrl(data.xlsx_url)}
						target="_blank"
						rel="noreferrer"
					>
						中間xlsx をダウンロード
					</a>
				</li>
			</ul>

			{data.type === "weekly" ? (
				<WeeklyArticles period={period} />
			) : (
				<MailPreview
					title="月刊ビリーフ（メール版 HTML）"
					url={data.html_urls[0]?.url ?? null}
				/>
			)}
		</section>
	);
}

function WeeklyArticles({ period }: { period: string }) {
	const [industry, setIndustry] = useState<string | null>(null);

	const articles = useQuery({
		queryKey: reportKeys.articles(period, industry),
		queryFn: () => fetchArticles(period, industry ?? undefined)
	});

	if (articles.isPending) {
		return <p className="mt-6 text-sm">記事を読み込み中…</p>;
	}
	if (articles.isError) {
		return (
			<Alert variant="destructive" className="mt-6">
				<AlertDescription>{toDisplayMessage(articles.error)}</AlertDescription>
			</Alert>
		);
	}

	const data = articles.data;
	return (
		<div className="mt-6">
			{data.industries.length > 1 && (
				<fieldset className="mb-4 flex flex-wrap gap-2">
					<legend className="sr-only">業界版</legend>
					{data.industries.map((name) => (
						<Button
							key={name}
							type="button"
							size="sm"
							variant={name === data.industry ? "default" : "outline"}
							aria-pressed={name === data.industry}
							onClick={() => setIndustry(name)}
						>
							{name} 版
						</Button>
					))}
				</fieldset>
			)}

			{data.point_of_week !== null && (
				<div className="rounded border p-4">
					<h3 className="text-sm font-semibold">今週のポイント</h3>
					<p className="mt-2 text-sm leading-relaxed whitespace-pre-line">
						{data.point_of_week}
					</p>
				</div>
			)}

			{data.sections.map((section) => (
				<div key={section.heading} className="mt-6">
					<h3 className="text-base font-semibold">{section.heading}</h3>
					<ul className="mt-2 space-y-2">
						{/* 鍵は URL（示唆を引く鍵でもあり、号の中で一意。§12.1 の
						    非空必須項目）。使えない URL の記事は見出しで代用する。 */}
						{section.articles.map((article) => (
							<li key={article.url ?? article.title}>
								<ArticleRow article={article} />
							</li>
						))}
					</ul>
				</div>
			))}
		</div>
	);
}

/** 出典行の文言（メール版 `weekly_renderer.SOURCE_LINE_FORMAT` と対）。 */
const SOURCE_PREFIX = "出典：";
/** 出典行に続く記事リンクの文言（メール版 `READ_MORE_LABEL` と対）。 */
export const READ_MORE_LABEL = "記事を読む";

/**
 * 記事1件のトグル行。
 *
 * 閉じているときはメール版と同じ密度（バッジ＋見出し＋出典）で、開くと**要約
 * （切っていない全文）と示唆**が出る。⚠️ `<details>` ではなく `aria-expanded` を
 * 持つボタンにしてあるのは、開閉の状態をテストとスクリーンリーダーの両方から
 * 同じ形で読めるようにするため。
 *
 * ⚠️ **見出しはリンクにしない**（T-50）。記事へのリンクは出典行が持つ
 * （`出典：〈ソース〉（記事を読む）`）。メール版（`weekly_renderer._card()` /
 * `_source_line()`）と同じ形にしてあるので、片方だけ直さないこと。
 */
function ArticleRow({ article }: { article: ArticleCardData }) {
	const [open, setOpen] = useState(false);
	const hasDetail =
		article.summary !== "" ||
		article.insight !== null ||
		article.diagram !== null;

	return (
		<div className="rounded border p-3">
			<div className="flex flex-wrap items-center gap-2">
				<span
					className="rounded px-2 py-0.5 text-[10px] font-bold text-white"
					// カテゴリ色は §7.2 の確定マップ（サーバーが解決した値をそのまま
					// 使う。フロントに色の写しを持たない）。
					style={{ backgroundColor: article.category_color }}
				>
					{article.category_label}
				</span>
			</div>

			{/* ⚠️ プレーン見出し（下線なし・一回り大きく）。T-50。 */}
			<h4 className="mt-2 text-base font-semibold leading-snug">
				{article.title}
			</h4>

			<p className="mt-1 text-xs text-muted-foreground">
				{SOURCE_PREFIX}
				{article.source}
				{/* ⚠️ `http`/`https` でない URL はサーバーが `null` にして返す
				    （メール版 `safe_url()` と同じ判定）。括弧ごと出さない。 */}
				{article.url !== null && (
					<>
						（
						<a
							className="underline"
							href={article.url}
							target="_blank"
							rel="noreferrer"
						>
							{READ_MORE_LABEL}
						</a>
						）
					</>
				)}
			</p>

			{hasDetail && (
				<>
					<button
						type="button"
						onClick={() => setOpen((value) => !value)}
						aria-expanded={open}
						className="mt-2 text-xs underline"
					>
						{open ? "要約と示唆を閉じる" : "要約と示唆を開く"}
					</button>

					{open && (
						<div className="mt-2 space-y-2">
							{article.summary !== "" && (
								<p className="text-sm leading-relaxed">{article.summary}</p>
							)}
							{article.insight !== null && (
								<p className="rounded border-l-4 border-indigo-500 bg-indigo-50 p-2 text-sm leading-relaxed">
									{article.insight}
								</p>
							)}
							{/* ⚠️ 図解が読めるのはここだけ（メール版は描かない＝T-49）。 */}
							{article.diagram !== null && (
								<DiagramView diagram={article.diagram} />
							)}
						</div>
					)}
				</>
			)}
		</div>
	);
}

/** 図解パネルの見出し（メール版 `monthly_renderer.DIAGRAM_EYEBROW` と対）。 */
export const DIAGRAM_LABEL = "図解";

/** `flow` のステップを繋ぐ矢印（メール版 `FLOW_ARROW` と対）。 */
const FLOW_ARROW = "→";

/**
 * 図解（T-49）。
 *
 * ⚠️ **描き方はここが決める。** サーバーが返すのは3タイプの構造化データだけで、
 * HTML の断片ではない（`api/reports.ts` の `diagramSchema`）。タイプごとの
 * 描き分けは `switch` で閉じてあり、**未知のタイプは型として存在しない**。
 *
 * ⚠️ メール版（`monthly_renderer`）は table＋inline style の制約があるが、
 * こちらは Web なので通常の CSS を使う。**同じデータの別の描き方**であって、
 * 内容は同じ。
 */
function DiagramView({ diagram }: { diagram: Diagram }) {
	return (
		<figure className="rounded border bg-slate-50 p-3">
			<figcaption className="text-xs font-semibold text-slate-600">
				<span className="mr-2 text-[10px] tracking-widest text-slate-400">
					{DIAGRAM_LABEL}
				</span>
				{diagram.title}
			</figcaption>
			<div className="mt-2">
				<DiagramBody diagram={diagram} />
			</div>
		</figure>
	);
}

function DiagramBody({ diagram }: { diagram: Diagram }) {
	switch (diagram.type) {
		case "flow":
			return (
				<ol className="flex flex-wrap items-center gap-2">
					{diagram.steps.map((step, index) => (
						<li key={step} className="flex items-center gap-2">
							{index > 0 && (
								<span aria-hidden="true" className="text-sky-600">
									{FLOW_ARROW}
								</span>
							)}
							<span className="rounded border border-sky-200 bg-white px-2 py-1 text-xs">
								{step}
							</span>
						</li>
					))}
				</ol>
			);
		case "compare":
			return (
				<div className="grid grid-cols-2 gap-2">
					{[diagram.left, diagram.right].map((pane) => (
						<div key={pane.label} className="rounded border bg-white">
							<p className="rounded-t bg-slate-700 px-2 py-1 text-xs font-semibold text-white">
								{pane.label}
							</p>
							<ul className="space-y-1 p-2 text-xs leading-relaxed">
								{pane.points.map((point) => (
									<li key={point}>{point}</li>
								))}
							</ul>
						</div>
					))}
				</div>
			);
		case "metrics":
			return (
				<dl className="flex flex-wrap gap-2">
					{diagram.items.map((item) => (
						<div
							key={`${item.value}-${item.label}`}
							className="flex-1 rounded border border-sky-200 bg-white p-2 text-center"
						>
							<dd className="text-lg font-bold text-slate-800">{item.value}</dd>
							<dt className="mt-1 text-[11px] text-muted-foreground">
								{item.label}
							</dt>
						</div>
					))}
				</dl>
			);
	}
}

/** メール版 HTML の埋め込み（配信される形をそのまま見る）。 */
function MailPreview({ title, url }: { title: string; url: string | null }) {
	if (url === null) {
		return (
			<p className="mt-6 text-sm text-muted-foreground">
				この号の HTML はまだありません。
			</p>
		);
	}

	return (
		<div className="mt-6">
			<h3 className="text-base font-semibold">{title}</h3>
			<iframe
				title={title}
				src={artifactUrl(url)}
				className="mt-2 h-[70vh] w-full rounded border"
			/>
		</div>
	);
}
