import type * as React from "react";

import { cn } from "@/lib/utils";

// shadcn の生成物は `radix-ui`（umbrella）の `Label.Root` を使うが、依存を増やさない
// ため素の `<label>` にしてある（TASKS.md T-43 実績）。`htmlFor` による
// 「ラベルを押すと入力へフォーカス」はブラウザ標準の挙動なので失われない。
// Radix 版との差はダブルクリック時のテキスト選択抑止だけ。
function Label({ className, ...props }: React.ComponentProps<"label">) {
	// 汎用プリミティブなので、対応する入力との紐付け（htmlFor）は呼び出し側が渡す。
	// ⚠️ 利用箇所では htmlFor と Input の id を必ず対にすること（LoginPage / RegisterPage 参照）。
	return (
		// biome-ignore lint/a11y/noLabelWithoutControl: htmlFor は呼び出し側が渡す
		<label
			data-slot="label"
			className={cn(
				"flex items-center gap-2 text-sm leading-none font-medium select-none group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-50 peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
				className
			)}
			{...props}
		/>
	);
}

export { Label };
