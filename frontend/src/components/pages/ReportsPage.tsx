export function ReportsPage() {
	return (
		<div className="p-6">
			<h1 className="text-xl font-semibold">レポート一覧</h1>
			<p className="text-sm text-neutral-500">
				週刊メルマガ・月刊ビリーフの生成結果をここに表示する（GET /reports/
				{"{period}"}）。
			</p>
		</div>
	);
}
