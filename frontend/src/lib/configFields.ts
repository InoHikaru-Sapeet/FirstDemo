/**
 * 編集できる項目の定義と、patch・差分の組み立て（T-33・T-34）。
 *
 * ⚠️ **許可リストの正はサーバー**（`application/usecases/update_config.py` の
 * `EDITABLE_PATHS`）。ここにある一覧は**画面に出す項目**であって、許可判定では
 * ない。ここに無い項目を patch へ入れてもサーバーが 422 で弾くし、逆にここに
 * 足しただけでは通らない。**判定を二重に持たない**のがこの分担の要点。
 *
 * ⚠️ **今日の範囲は `tunable_thresholds` だけ**（スコア軸の配点・除外ルールの
 * 有効/無効と強度・カテゴリ優先度は未実装。T-33 の残り）。`EDITABLE_PATHS` は
 * それらも許しているので、足すのはこの表への追記＋入力欄の種類の追加になる。
 *
 * ⚠️ **ID系（`information_categories[].id` 等）は編集しない**（仕様書 §5.1 の
 * 確定値。サーバーもセレクタとしてしか見ない）。
 */

/** 入力欄の種類。 */
export type FieldKind = "int" | "ratio" | "bool" | "industries";

export type FieldDescriptor = {
	/** `tunable_thresholds.weekly.max_common_topics` のようなドット区切り。
	 *  ⚠️ **サーバーの `ConfigIssue.path` と同じ表記**（422 をフォーム欄へ
	 *  対応づける鍵になる）。 */
	path: string;
	label: string;
	kind: FieldKind;
	/** 何に効く値かの1行説明（管理者が値を触る前に読むもの）。 */
	hint?: string;
};

export type FieldGroup = {
	title: string;
	fields: FieldDescriptor[];
};

/**
 * 画面に出す項目（仕様書 §7.2 の可変項目のうち `tunable_thresholds` 配下）。
 *
 * 並びは「採否に効くもの → 週刊 → 月刊 → 重複判定」。§5.2 の JSON の並びでは
 * なく、admin が触る頻度と因果の近さで並べてある。
 */
export const FIELD_GROUPS: FieldGroup[] = [
	{
		title: "採否のしきい値",
		fields: [
			{
				path: "tunable_thresholds.min_total_score_to_publish",
				label: "掲載最低スコア",
				kind: "int",
				hint: "この点数未満の記事はレポートに載らない（0〜100）。"
			},
			{
				path: "tunable_thresholds.min_reliability_score_to_publish",
				label: "掲載最低の信頼性スコア",
				kind: "int",
				hint: "信頼性軸の点がこの値未満なら載らない（0〜10）。"
			},
			{
				path: "tunable_thresholds.adoption_class_score_map.propose_next_meeting",
				label: "採用区分：次回定例で提案",
				kind: "int",
				hint: "この点数以上を「次回定例で提案」に分類する。"
			},
			{
				path: "tunable_thresholds.adoption_class_score_map.reference_info",
				label: "採用区分：参考情報",
				kind: "int"
			},
			{
				path: "tunable_thresholds.adoption_class_score_map.share_only",
				label: "採用区分：共有のみ",
				kind: "int",
				hint: "⚠️ 3つは降順（提案 ≥ 参考 ≥ 共有 ≥ 掲載最低スコア）でないと保存できない。"
			}
		]
	},
	{
		title: "週刊メルマガ",
		fields: [
			{
				path: "tunable_thresholds.weekly.target_industries",
				label: "対象業界",
				kind: "industries",
				hint: "⚠️ 選んだ業界の数だけメールが出る（業界ごとに1通）。1つ以上必須。"
			},
			{
				path: "tunable_thresholds.weekly.max_industry_topics",
				label: "業界関連トピックの上限",
				kind: "int"
			},
			{
				path: "tunable_thresholds.weekly.max_common_topics",
				label: "業界共通トピックの上限",
				kind: "int"
			},
			{
				path: "tunable_thresholds.weekly.point_of_week_required",
				label: "「今週のポイント」を必須にする",
				kind: "bool",
				hint: "必須にすると、生成されなかった週は HTML の生成が失敗する（黙って省かない）。"
			}
		]
	},
	{
		title: "月刊ビリーフ",
		fields: [
			{
				path: "tunable_thresholds.monthly.target_case_count",
				label: "目標事例数",
				kind: "int",
				hint: "構成の目安（超えても出力は切り詰めない）。"
			},
			{
				path: "tunable_thresholds.monthly.chapter_count_hint",
				label: "章数の目安",
				kind: "int"
			},
			{
				path: "tunable_thresholds.monthly.min_score_for_case",
				label: "事例に採る最低スコア",
				kind: "int",
				hint: "⚠️ 実測の採点は 60〜73 点に密集する（初運用の実績）。高すぎると事例が0件になる。"
			},
			{
				path: "tunable_thresholds.monthly.require_editorial_and_closing",
				label: "巻頭言・むすびを必須にする",
				kind: "bool"
			}
		]
	},
	{
		title: "重複判定",
		fields: [
			{
				path: "tunable_thresholds.dedup.lookback_weeks",
				label: "週次の遡り週数",
				kind: "int"
			},
			{
				path: "tunable_thresholds.dedup.monthly_lookback_months",
				label: "月次の遡り月数",
				kind: "int"
			},
			{
				path: "tunable_thresholds.dedup.title_similarity_threshold",
				label: "タイトル類似度のしきい値",
				kind: "ratio",
				hint: "0〜1。これ以上似ていたら同じ記事とみなす。"
			},
			{
				path: "tunable_thresholds.dedup.treat_same_url_as_duplicate",
				label: "同一 URL を重複として扱う",
				kind: "bool"
			}
		]
	}
];

export const ALL_FIELDS: FieldDescriptor[] = FIELD_GROUPS.flatMap(
	(group) => group.fields
);

/** フォームが持つ値（入力中は文字列のことがある）。 */
export type FieldValue = number | boolean | string[] | string;

export type FormValues = Record<string, FieldValue>;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** ドット区切りのパスで値を引く（`as` キャストを使わない）。 */
export function valueAt(source: unknown, path: string): unknown {
	let current: unknown = source;
	for (const key of path.split(".")) {
		if (!isRecord(current)) {
			return undefined;
		}
		current = current[key];
	}
	return current;
}

/** config から、画面が編集する項目だけを取り出す。 */
export function toFormValues(config: unknown): FormValues {
	const values: FormValues = {};
	for (const field of ALL_FIELDS) {
		const raw = valueAt(config, field.path);
		if (field.kind === "industries") {
			values[field.path] = Array.isArray(raw) ? raw.map(String) : [];
		} else if (field.kind === "bool") {
			values[field.path] = raw === true;
		} else if (typeof raw === "number") {
			values[field.path] = raw;
		} else {
			values[field.path] = "";
		}
	}
	return values;
}

/** 入力値を送る形へ直す（数値欄は数へ。読めない入力はそのまま送ってサーバーに判定させる）。 */
function normalize(field: FieldDescriptor, value: FieldValue): unknown {
	if (field.kind === "bool" || field.kind === "industries") {
		return value;
	}
	if (typeof value !== "string") {
		// 数値欄に数値・文字列以外は入らない（`toFormValues` が保証する）。
		return value;
	}
	const parsed =
		field.kind === "ratio" ? Number(value) : Number.parseInt(value, 10);
	// ⚠️ **読めない入力を 0 や既定値へ落とさない。** 落とすと「入力ミスが黙って
	// 別の値として保存される」ので、そのまま送って 422 を受ける。
	return Number.isNaN(parsed) ? value : parsed;
}

function sameValue(a: unknown, b: unknown): boolean {
	if (Array.isArray(a) && Array.isArray(b)) {
		// 対象業界は集合として比べる（並べ替えだけの差分を「変更」にしない）。
		return (
			a.length === b.length &&
			[...a].sort().every((item, index) => item === [...b].sort()[index])
		);
	}
	return a === b;
}

export type FieldChange = {
	field: FieldDescriptor;
	before: unknown;
	after: unknown;
};

/** 変更された項目（差分プレビューの素。T-34）。 */
export function changedFields(
	config: unknown,
	values: FormValues
): FieldChange[] {
	const changes: FieldChange[] = [];
	for (const field of ALL_FIELDS) {
		const before = valueAt(config, field.path);
		const after = normalize(field, values[field.path]);
		if (!sameValue(before, after)) {
			changes.push({ field, before, after });
		}
	}
	return changes;
}

/**
 * 変更ぶんだけを入れ子の patch へ組む（`PUT /config` / `POST /config/dry-run`）。
 *
 * ⚠️ **変更していない項目は入れない。** 全体を送ると、他の管理者が同時に変えた
 * 項目を「元の値へ戻す」patch になってしまう（revision の突合を通ってしまう
 * 範囲でも、意図しない巻き戻しが起きる）。
 */
export function buildPatch(changes: FieldChange[]): Record<string, unknown> {
	const patch: Record<string, unknown> = {};
	for (const change of changes) {
		const keys = change.field.path.split(".");
		let cursor = patch;
		for (const key of keys.slice(0, -1)) {
			const next = cursor[key];
			if (!isRecord(next)) {
				cursor[key] = {};
			}
			const child = cursor[key];
			cursor = isRecord(child) ? child : cursor;
		}
		cursor[keys[keys.length - 1]] = change.after;
	}
	return patch;
}

/** 差分プレビューの表示用（before → after のテキスト）。 */
export function formatValue(value: unknown): string {
	if (value === undefined || value === null) {
		return "（未設定）";
	}
	if (typeof value === "boolean") {
		return value ? "有効" : "無効";
	}
	if (Array.isArray(value)) {
		return value.length === 0 ? "（なし）" : value.join("・");
	}
	return String(value);
}
