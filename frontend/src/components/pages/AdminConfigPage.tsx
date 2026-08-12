// 設計書§5「管理画面設計」/ 設計判断D：管理専用サブ画面（/admin/config）
// admin以外はサーバ側403で弾かれる前提（フロント側の非表示は補助）。
export function AdminConfigPage() {
	return (
		<div className="p-6">
			<h1 className="text-xl font-semibold">判断基準（管理者）</h1>
			<p className="text-sm text-neutral-500">
				config.json
				の編集フォーム・差分プレビュー・ドライラン再フィルタをここに実装する。
			</p>
		</div>
	);
}
