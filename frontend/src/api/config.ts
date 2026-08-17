/**
 * 判断基準 API（バックエンド T-12・T-13・T-29 の `routers/config.py`）。
 *
 * ⚠️ **admin 限定**（仕様書 §6.2）。非 admin は 403 で、**存在も中身も返らない**
 * （§6.1）。フロントは 403 を「権限がありません」として**画面内で**処理し、
 * ログイン画面へ飛ばさない（それは 401 の扱い。`api/client.ts`）。
 *
 * ---
 *
 * ⚠️ **`config` の型は「触る範囲だけ」を書いた部分スキーマ。**
 *
 * T-31（OpenAPI からの型生成）が未着手なので手書きだが、config 全体（7カテゴリ・
 * 10必須タグ・6軸・13除外ルール・enum）を写すのは**確定値の二重管理**になる。
 * この画面が編集するのは `tunable_thresholds` だけなので、**zod は編集する枝を
 * 厳密に読み、残りは素通しする**（`.passthrough()` ではなく、読まない枝を
 * スキーマに書かない）。
 *
 * ⚠️ **`revision` はサーバーが決める**。フォームは hidden で保持して
 * `base_revision` として送り返すだけ（楽観ロック。競合は 409）。
 *
 * ---
 *
 * ⚠️ **保存の最終権限はサーバー**（設計判断A）。
 *
 * 配点合計100・しきい値の降順整合は `ConfigRepository.save()` が 422 で拒否し、
 * **値を自動補正しない**。フロントの一次チェックは「422 を待たずに気づける」
 * ための補助であって、通ってしまった入力を通す根拠にはならない。
 */

import { z } from "zod";
import { apiJson } from "@/api/client";

/** 除外ルールの強度（仕様書 §5.4 の5値）。 */
export const SEVERITIES = [
	"完全除外",
	"条件付き除外",
	"減点のみ",
	"要確認",
	"除外しない"
] as const;

const adoptionClassScoreMapSchema = z.object({
	propose_next_meeting: z.number().int(),
	reference_info: z.number().int(),
	share_only: z.number().int()
});

const weeklyThresholdsSchema = z.object({
	target_industries: z.array(z.string()),
	max_industry_topics: z.number().int(),
	max_common_topics: z.number().int(),
	point_of_week_required: z.boolean()
});

const monthlyThresholdsSchema = z.object({
	target_case_count: z.number().int(),
	chapter_count_hint: z.number().int(),
	min_score_for_case: z.number().int(),
	require_editorial_and_closing: z.boolean()
});

const dedupThresholdsSchema = z.object({
	lookback_weeks: z.number().int(),
	monthly_lookback_months: z.number().int(),
	title_similarity_threshold: z.number(),
	treat_same_url_as_duplicate: z.boolean()
});

export const tunableThresholdsSchema = z.object({
	min_total_score_to_publish: z.number().int(),
	min_reliability_score_to_publish: z.number().int(),
	adoption_class_score_map: adoptionClassScoreMapSchema,
	weekly: weeklyThresholdsSchema,
	monthly: monthlyThresholdsSchema,
	dedup: dedupThresholdsSchema
});
export type TunableThresholds = z.infer<typeof tunableThresholdsSchema>;

/**
 * `ConfigResponse` のうちこの画面が読む枝。
 *
 * ⚠️ `enums.industry` は**週刊の対象業界の選択肢**（参照整合はサーバーが見る。
 * 設計書 §2.1.1-3）。運用で増減しうるので自由文字列のリスト。
 */
export const configSchema = z.object({
	revision: z.number().int(),
	config: z.object({
		tunable_thresholds: tunableThresholdsSchema,
		enums: z.object({ industry: z.array(z.string()) })
	})
});
export type ConfigDocument = z.infer<typeof configSchema>;

export const updateConfigResponseSchema = z.object({
	revision: z.number().int(),
	updated_at: z.string().nullable(),
	updated_by: z.string().nullable()
});
export type UpdateConfigResponse = z.infer<typeof updateConfigResponseSchema>;

const dryRunCountsSchema = z.object({
	adopted: z.number().int(),
	excluded: z.number().int()
});

export const dryRunResponseSchema = z.object({
	dry_run_id: z.string(),
	period: z.string(),
	base_revision: z.number().int(),
	scratch_url: z.string(),
	summary: dryRunCountsSchema,
	baseline: dryRunCountsSchema,
	ttl_hours: z.number().int()
});
export type DryRunResponse = z.infer<typeof dryRunResponseSchema>;

/** patch の中身（許可リストの判定はサーバー＝`EDITABLE_PATHS` が正）。 */
export type ConfigPatch = Record<string, unknown>;

/** `GET /config`（admin のみ）。 */
export async function fetchConfig(): Promise<ConfigDocument> {
	return apiJson("/config", configSchema);
}

/**
 * `PUT /config`（admin のみ）。
 *
 * @param baseRevision 編集を始めた時点の revision（**楽観ロック**。他の管理者が
 *   先に保存していたら 409 が返る）
 */
export async function updateConfig(
	baseRevision: number,
	patch: ConfigPatch
): Promise<UpdateConfigResponse> {
	return apiJson("/config", updateConfigResponseSchema, {
		method: "PUT",
		body: { base_revision: baseRevision, patch }
	});
}

/**
 * `POST /config/dry-run`（admin のみ）→ 202。
 *
 * ⚠️ **実ファイルは上書きされない**（結果は `scratch/dry-run/{id}/` に隔離され、
 * TTL 後に消える。設計判断C）。
 *
 * ⚠️ **適用できるのは既存の採点結果へ決定的に再適用できる変更だけ**（掲載
 * しきい値・信頼性しきい値・採用区分しきい値）。それ以外は 422 `not_previewable`
 * で断られる——「効果ゼロ」として黙って通さないのが要点なので、この 422 は
 * サーバーの文言をそのまま出す。
 */
export async function runDryRun(
	period: string,
	patch: ConfigPatch,
	baseRevision?: number
): Promise<DryRunResponse> {
	return apiJson("/config/dry-run", dryRunResponseSchema, {
		method: "POST",
		body: {
			period,
			candidate_config_patch: patch,
			...(baseRevision === undefined ? {} : { base_revision: baseRevision })
		}
	});
}
