/**
 * 判断基準（`config.json`）の編集画面（T-32・T-33・T-34・T-35 の最小実装）。
 *
 * 設計書 §5・設計判断D の**管理専用サブ画面 `/admin/config`**。週刊／月刊いずれの
 * ナビからも同じルートへ来る（実体は単一モジュール）。
 *
 * ---
 *
 * ⚠️ **admin 以外はここへ来ても何も見えない。**
 *
 * 実体はサーバーの 403（`require_admin`）で、config は**存在も中身も返らない**
 * （仕様書 §6.1）。この画面は 403 を受けたら「権限がありません」だけを出し、
 * **config に触れる項目名も既定値も画面に出さない**（T-32 完了条件）。
 * ナビのリンクを admin だけに出すのは補助（`AdminNavLink`）。
 *
 * ⚠️ **403 でログイン画面へ飛ばさない**（それは 401 の扱い。再ログインしても
 * 権限は変わらないので、飛ばすと無限に行き来する）。
 *
 * ---
 *
 * ⚠️ **保存の最終権限はサーバー**（設計判断A）。
 *
 * 配点合計100・しきい値の降順整合は `PUT /config` が 422 で拒否し、**値を自動
 * 補正しない**。この画面は 422 の `issues[].path` を該当欄の下へ出すところまでを
 * 担い、**入力を直すのは admin**。フロントで正規化して通そうとしないこと。
 *
 * ⚠️ **`revision` は hidden 保持**（`base_revision` として送り返す＝楽観ロック）。
 * 409 が返ったら「他の管理者が更新しました」とサーバーの現行 revision を出す。
 *
 * ---
 *
 * **今日の範囲（T-33 の残り）**
 *
 * 編集できるのは **`tunable_thresholds` だけ**。スコア軸の配点（`scoring_axes[]
 * .weight`）・除外ルールの有効/無効と強度・カテゴリ優先度は未実装で、
 * サーバー側（`EDITABLE_PATHS`）は許しているので**入力欄を足せば通る**。
 * 「比率維持で100へ補正」ボタン（設計判断A）も配点フォームと同時に入れる。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { isApiError, isForbidden, toDisplayMessage } from "@/api/client";
import {
	type ConfigDocument,
	type DryRunResponse,
	fetchConfig,
	runDryRun,
	updateConfig
} from "@/api/config";
import { configKeys } from "@/api/query-keys";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
	buildPatch,
	changedFields,
	FIELD_GROUPS,
	type FieldChange,
	type FieldDescriptor,
	type FormValues,
	formatValue,
	toFormValues
} from "@/lib/configFields";

/** 403 のときに出す唯一の文（⚠️ config の項目名を含めない）。 */
export const FORBIDDEN_MESSAGE =
	"この画面を表示する権限がありません。管理者に依頼してください。";

export function AdminConfigPage() {
	// ⚠️ **保存の成否はフォームの外に置く。** フォームは revision を `key` に
	// して作り直されるので、成功メッセージを中に持たせると保存が通った瞬間に
	// 消えてしまう（何が起きたのか admin に伝わらない）。
	const [savedRevision, setSavedRevision] = useState<number | null>(null);

	const query = useQuery({
		queryKey: configKeys.current(),
		queryFn: fetchConfig,
		// ⚠️ 403 で再試行しない（権限は再試行では変わらない）。
		retry: false
	});

	if (query.isPending) {
		return <Frame>読み込み中…</Frame>;
	}

	if (query.isError) {
		return (
			<Frame>
				<Alert variant="destructive">
					<AlertDescription>
						{isForbidden(query.error)
							? FORBIDDEN_MESSAGE
							: toDisplayMessage(query.error)}
					</AlertDescription>
				</Alert>
			</Frame>
		);
	}

	// ⚠️ `key` に revision を入れて、保存が通ったらフォームを作り直す
	// （新しい revision を初期値として取り込む。T-34 完了条件）。
	return (
		<Frame>
			{savedRevision !== null && (
				<Alert className="mb-4">
					<AlertDescription>
						保存しました（revision {savedRevision}）。
					</AlertDescription>
				</Alert>
			)}
			<ConfigForm
				key={query.data.revision}
				document={query.data}
				onSaved={setSavedRevision}
			/>
		</Frame>
	);
}

function Frame({ children }: { children: React.ReactNode }) {
	return (
		<main className="mx-auto max-w-3xl p-6">
			<h1 className="text-xl font-semibold">判断基準（管理者）</h1>
			<p className="mt-1 text-sm text-muted-foreground">
				レポートの採否と構成を決める値を編集します。
			</p>
			<div className="mt-6">{children}</div>
		</main>
	);
}

type ConfigFormProps = {
	document: ConfigDocument;
	onSaved: (revision: number) => void;
};

function ConfigForm({ document, onSaved }: ConfigFormProps) {
	const queryClient = useQueryClient();
	const config = document.config;
	const [values, setValues] = useState<FormValues>(() => toFormValues(config));

	const changes = changedFields(config, values);

	const save = useMutation({
		mutationFn: () => updateConfig(document.revision, buildPatch(changes)),
		onSuccess: async (result) => {
			onSaved(result.revision);
			// 再取得でフォームが作り直される（`key={revision}`）。
			await queryClient.invalidateQueries({ queryKey: configKeys.all });
		}
	});

	const issues = isApiError(save.error) ? save.error.issues : [];
	const issueFor = (path: string): string | null => {
		const hit = issues.find((issue) => issue.path === path);
		return hit?.reason ?? null;
	};

	return (
		<form
			onSubmit={(event) => {
				event.preventDefault();
				save.mutate();
			}}
		>
			{/* ⚠️ revision は表示のみ・編集不可（送るのは `base_revision`）。 */}
			<p className="text-sm text-muted-foreground">
				現在の revision: <strong>{document.revision}</strong>
			</p>

			<SaveError error={save.error} />

			{FIELD_GROUPS.map((group) => (
				<fieldset key={group.title} className="mt-6">
					<legend className="text-base font-semibold">{group.title}</legend>
					<div className="mt-3 space-y-4">
						{group.fields.map((field) => (
							<Field
								key={field.path}
								field={field}
								value={values[field.path]}
								industries={config.enums.industry}
								issue={issueFor(field.path)}
								onChange={(next) =>
									setValues((current) => ({ ...current, [field.path]: next }))
								}
							/>
						))}
					</div>
				</fieldset>
			))}

			<DiffPreview changes={changes} />

			<div className="mt-6 flex flex-wrap gap-3">
				<Button type="submit" disabled={changes.length === 0 || save.isPending}>
					{save.isPending ? "保存中…" : "保存する"}
				</Button>
				<Button
					type="button"
					variant="outline"
					disabled={changes.length === 0}
					onClick={() => setValues(toFormValues(config))}
				>
					変更を破棄する
				</Button>
			</div>

			<DryRunPanel changes={changes} baseRevision={document.revision} />
		</form>
	);
}

/** 422 で `issues[]` が付いているときの総括（**理由は各欄の下に出す**）。 */
export const VALIDATION_SUMMARY =
	"保存できませんでした。各項目のメッセージを確認してください。";

function SaveError({ error }: { error: unknown }) {
	if (error === null || error === undefined) {
		return null;
	}

	// 409 = 他の管理者が先に保存した（T-34 完了条件）。
	if (isApiError(error) && error.status === 409) {
		return (
			<Alert variant="destructive" className="mt-4">
				<AlertDescription>
					他の管理者が更新しました。読み込み直してから編集してください。
					{typeof error.message === "string" && ` （${error.message}）`}
				</AlertDescription>
			</Alert>
		);
	}

	// ⚠️ **422 の理由を上と欄の両方へ二重に出さない。** 同じ文が2箇所に出ると、
	// 「どの欄の話か」を探す手間が増えるだけ。理由は欄の下（`Field` の `issue`）で、
	// ここは「保存できなかった」ことだけを伝える。
	const message =
		isApiError(error) && error.status === 422 && error.issues.length > 0
			? VALIDATION_SUMMARY
			: toDisplayMessage(error);

	return (
		<Alert variant="destructive" className="mt-4">
			<AlertDescription>{message}</AlertDescription>
		</Alert>
	);
}

type FieldProps = {
	field: FieldDescriptor;
	value: FormValues[string];
	industries: string[];
	issue: string | null;
	onChange: (value: FormValues[string]) => void;
};

function Field({ field, value, industries, issue, onChange }: FieldProps) {
	const id = field.path;

	return (
		<div>
			{field.kind === "bool" ? (
				<div className="flex items-center gap-2">
					<input
						id={id}
						type="checkbox"
						checked={value === true}
						onChange={(event) => onChange(event.target.checked)}
					/>
					<Label htmlFor={id}>{field.label}</Label>
				</div>
			) : field.kind === "industries" ? (
				<IndustryPicker
					field={field}
					selected={Array.isArray(value) ? value : []}
					options={industries}
					onChange={onChange}
				/>
			) : (
				<>
					<Label htmlFor={id}>{field.label}</Label>
					<Input
						id={id}
						type="number"
						inputMode="decimal"
						step={field.kind === "ratio" ? "0.01" : "1"}
						value={
							typeof value === "number" || typeof value === "string"
								? value
								: ""
						}
						onChange={(event) => onChange(event.target.value)}
						className="mt-1 max-w-40"
					/>
				</>
			)}

			{field.hint !== undefined && (
				<p className="mt-1 text-xs text-muted-foreground">{field.hint}</p>
			)}
			{issue !== null && (
				<p role="alert" className="mt-1 text-xs text-destructive">
					{issue}
				</p>
			)}
		</div>
	);
}

type IndustryPickerProps = {
	field: FieldDescriptor;
	selected: string[];
	options: string[];
	onChange: (value: string[]) => void;
};

/**
 * 対象業界の複数選択（T-46 Step 3 で単数 → 複数になった項目）。
 *
 * ⚠️ **選択肢は config の `enums.industry`**（フロントに写しを持たない）。
 * 参照整合と重複禁止はサーバーが見る（設計書 §2.1.1-3）。
 */
function IndustryPicker({
	field,
	selected,
	options,
	onChange
}: IndustryPickerProps) {
	return (
		<fieldset>
			<legend className="text-sm font-medium">{field.label}</legend>
			<div className="mt-2 flex flex-wrap gap-x-4 gap-y-2">
				{options.map((option) => {
					const id = `${field.path}--${option}`;
					const checked = selected.includes(option);
					return (
						<div key={option} className="flex items-center gap-1">
							<input
								id={id}
								type="checkbox"
								checked={checked}
								onChange={() =>
									onChange(
										checked
											? selected.filter((item) => item !== option)
											: [...selected, option]
									)
								}
							/>
							<Label htmlFor={id} className="text-sm font-normal">
								{option}
							</Label>
						</div>
					);
				})}
			</div>
		</fieldset>
	);
}

/**
 * 差分プレビュー（T-34）。**before → after のテキスト表示**。
 *
 * リッチな UI（並記のカラム・ハイライト）は後日。ここで大事なのは
 * 「何が何から何へ変わるのか」が保存の前に読めることだけ。
 */
function DiffPreview({ changes }: { changes: FieldChange[] }) {
	return (
		<section className="mt-8" aria-label="変更内容">
			<h2 className="text-base font-semibold">変更内容</h2>
			{changes.length === 0 ? (
				<p className="mt-2 text-sm text-muted-foreground">変更はありません。</p>
			) : (
				<ul className="mt-2 space-y-1 text-sm">
					{changes.map((change) => (
						<li key={change.field.path}>
							<span className="font-medium">{change.field.label}</span>：
							<span>{formatValue(change.before)}</span>
							{" → "}
							<strong>{formatValue(change.after)}</strong>
							<span className="ml-2 text-xs text-muted-foreground">
								{change.field.path}
							</span>
						</li>
					))}
				</ul>
			)}
		</section>
	);
}

/** ドライランの説明（⚠️ 実ファイルを触らないこと・TTL を UI に明示する）。 */
export const DRY_RUN_NOTICE =
	"ドライランは実ファイルを上書きしません。結果は一時領域に隔離され、TTL 経過後に削除されます。";

/**
 * ドライラン（T-35 ／ 設計判断C）。
 *
 * ⚠️ **未保存の変更に対して試算する**（保存の前に件数の増減を見るための機能）。
 * 変更が無ければ押せない——同じ config で試算しても baseline と同じ数が出るだけ。
 *
 * ⚠️ **422 `not_previewable` はサーバーの文言をそのまま出す。** 「この変更は
 * 件数に影響しません」と読める言い換えをしてはいけない（実際には**試算できない**
 * のであって、効果が無いわけではない）。
 */
function DryRunPanel({
	changes,
	baseRevision
}: {
	changes: FieldChange[];
	baseRevision: number;
}) {
	const [period, setPeriod] = useState("");

	const dryRun = useMutation<DryRunResponse>({
		mutationFn: () => runDryRun(period, buildPatch(changes), baseRevision)
	});

	const disabled =
		changes.length === 0 || period.trim() === "" || dryRun.isPending;

	return (
		<section className="mt-10 rounded border p-4" aria-label="ドライラン">
			<h2 className="text-base font-semibold">
				この基準で再フィルタ（ドライラン）
			</h2>
			<p className="mt-1 text-xs text-muted-foreground">{DRY_RUN_NOTICE}</p>

			<div className="mt-3 flex flex-wrap items-end gap-3">
				<div>
					<Label htmlFor="dry-run-period">対象期間</Label>
					<Input
						id="dry-run-period"
						value={period}
						placeholder="2026-W31"
						onChange={(event) => setPeriod(event.target.value)}
						className="mt-1 max-w-40"
					/>
				</div>
				<Button
					type="button"
					variant="outline"
					disabled={disabled}
					onClick={() => dryRun.mutate()}
				>
					{dryRun.isPending ? "試算中…" : "ドライランを実行"}
				</Button>
			</div>

			{changes.length === 0 && (
				<p className="mt-2 text-xs text-muted-foreground">
					変更を加えるとドライランを実行できます。
				</p>
			)}

			{dryRun.isError && (
				<Alert variant="destructive" className="mt-3">
					{/* ⚠️ サーバーの文言をそのまま（422 not_previewable を言い換えない）。 */}
					<AlertDescription>{toDisplayMessage(dryRun.error)}</AlertDescription>
				</Alert>
			)}

			{dryRun.isSuccess && <DryRunResult result={dryRun.data} />}
		</section>
	);
}

function DryRunResult({ result }: { result: DryRunResponse }) {
	return (
		<div className="mt-3 text-sm">
			<p>
				{result.period} の試算：採用 {result.baseline.adopted} →{" "}
				<strong>{result.summary.adopted}</strong> 件 ／ 除外{" "}
				{result.baseline.excluded} → <strong>{result.summary.excluded}</strong>{" "}
				件
			</p>
			<p className="mt-1">
				<a
					className="underline"
					href={`/api${result.scratch_url}`}
					target="_blank"
					rel="noreferrer"
				>
					明細（除外区分・理由つき）をダウンロード
				</a>
				<span className="ml-2 text-xs text-muted-foreground">
					{result.ttl_hours} 時間後に削除されます
				</span>
			</p>
		</div>
	);
}
