/**
 * 編集項目の定義と patch・差分の組み立て（T-33・T-34）。重点:
 *
 * - ⚠️ **変更ぶんだけを patch に入れる**（触っていない項目を巻き戻さない）
 * - ⚠️ **読めない入力を 0 へ落とさない**（サーバーの 422 で気づけるようにする）
 * - 表示している項目のパスが**サーバーの許可リストの表記と揃っている**こと
 */

import { describe, expect, it } from "vitest";
import {
	ALL_FIELDS,
	buildPatch,
	changedFields,
	type FormValues,
	formatValue,
	toFormValues,
	valueAt
} from "@/lib/configFields";

const CONFIG = {
	tunable_thresholds: {
		min_total_score_to_publish: 55,
		min_reliability_score_to_publish: 5,
		adoption_class_score_map: {
			propose_next_meeting: 85,
			reference_info: 70,
			share_only: 60
		},
		target_industries: ["不動産"],
		weekly: {
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
	}
};

function withChange(overrides: FormValues): FormValues {
	return { ...toFormValues(CONFIG), ...overrides };
}

describe("表示している項目", () => {
	it("⚠️ すべて `tunable_thresholds` 配下（今日の範囲）", () => {
		for (const field of ALL_FIELDS) {
			expect(field.path.startsWith("tunable_thresholds.")).toBe(true);
		}
	});

	it("パスが config に実在する（表記のずれを固定する）", () => {
		for (const field of ALL_FIELDS) {
			expect(valueAt(CONFIG, field.path)).toBeDefined();
		}
	});

	it("パスが重複していない", () => {
		const paths = ALL_FIELDS.map((field) => field.path);
		expect(new Set(paths).size).toBe(paths.length);
	});
});

describe("toFormValues", () => {
	it("数値・真偽・リストをそのまま取り込む", () => {
		const values = toFormValues(CONFIG);

		expect(values["tunable_thresholds.min_total_score_to_publish"]).toBe(55);
		expect(values["tunable_thresholds.weekly.point_of_week_required"]).toBe(
			true
		);
		expect(values["tunable_thresholds.target_industries"]).toEqual(["不動産"]);
	});
});

describe("changedFields", () => {
	it("何も触っていなければ空", () => {
		expect(changedFields(CONFIG, toFormValues(CONFIG))).toEqual([]);
	});

	it("数値欄の文字列入力を数として比べる", () => {
		const changes = changedFields(
			CONFIG,
			withChange({ "tunable_thresholds.min_total_score_to_publish": "62" })
		);

		expect(changes).toHaveLength(1);
		expect(changes[0].before).toBe(55);
		expect(changes[0].after).toBe(62);
	});

	it("同じ値を打ち直しただけなら変更にしない", () => {
		const changes = changedFields(
			CONFIG,
			withChange({ "tunable_thresholds.min_total_score_to_publish": "55" })
		);

		expect(changes).toEqual([]);
	});

	it("⚠️ 読めない入力を 0 へ落とさず、そのまま差分に出す", () => {
		const changes = changedFields(
			CONFIG,
			withChange({ "tunable_thresholds.min_total_score_to_publish": "" })
		);

		expect(changes).toHaveLength(1);
		// 0 に化けていたら「入力ミスが別の値として保存される」ことになる。
		expect(changes[0].after).toBe("");
	});

	it("小数のしきい値は小数として比べる", () => {
		const changes = changedFields(
			CONFIG,
			withChange({
				"tunable_thresholds.dedup.title_similarity_threshold": "0.9"
			})
		);

		expect(changes[0].after).toBe(0.9);
	});

	it("対象業界は集合として比べる（並べ替えだけは変更にしない）", () => {
		const reordered = changedFields(
			CONFIG,
			withChange({
				"tunable_thresholds.target_industries": ["不動産"]
			})
		);
		const added = changedFields(
			CONFIG,
			withChange({
				"tunable_thresholds.target_industries": ["金融", "不動産"]
			})
		);

		expect(reordered).toEqual([]);
		expect(added).toHaveLength(1);
	});
});

describe("buildPatch", () => {
	it("⚠️ 変更ぶんだけを入れ子で組む（触っていない項目を含めない）", () => {
		const changes = changedFields(
			CONFIG,
			withChange({
				"tunable_thresholds.min_total_score_to_publish": "50",
				"tunable_thresholds.weekly.max_common_topics": "10"
			})
		);

		expect(buildPatch(changes)).toEqual({
			tunable_thresholds: {
				min_total_score_to_publish: 50,
				weekly: { max_common_topics: 10 }
			}
		});
	});

	it("同じ枝の複数項目を1つのオブジェクトへまとめる", () => {
		const changes = changedFields(
			CONFIG,
			withChange({
				"tunable_thresholds.adoption_class_score_map.reference_info": "68",
				"tunable_thresholds.adoption_class_score_map.share_only": "58"
			})
		);

		expect(buildPatch(changes)).toEqual({
			tunable_thresholds: {
				adoption_class_score_map: { reference_info: 68, share_only: 58 }
			}
		});
	});

	it("変更が無ければ空の patch", () => {
		expect(buildPatch([])).toEqual({});
	});
});

describe("formatValue", () => {
	it("真偽・リスト・未設定を人が読める形にする", () => {
		expect(formatValue(true)).toBe("有効");
		expect(formatValue(false)).toBe("無効");
		expect(formatValue(["不動産", "金融"])).toBe("不動産・金融");
		expect(formatValue([])).toBe("（なし）");
		expect(formatValue(undefined)).toBe("（未設定）");
		expect(formatValue(0.85)).toBe("0.85");
	});
});
