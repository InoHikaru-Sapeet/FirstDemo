import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// バックエンド（FastAPI）の開発サーバー。ルーターは `/auth/...` のように
// プレフィックス無しで生えているので、proxy 側で `/api` を落とす。
const BACKEND_ORIGIN = "http://localhost:8000";

// https://vite.dev/config/
export default defineConfig({
	plugins: [react(), tailwindcss()],
	resolve: {
		alias: {
			"@": resolve(import.meta.dirname, "./src")
		}
	},
	server: {
		// ⚠️ この proxy はセッション Cookie が dev で届くための前提（TASKS.md T-40 備考）。
		// フロント（:5173）から直接 :8000 を叩くとオリジンが異なり、`SameSite=Lax` の
		// `sid` Cookie がリクエストに載らない＝ログインしても未認証扱いになる。
		// proxy を通せば**ブラウザから見て同一オリジン**になるので Cookie が届く。
		// `SameSite=None` + CORS credentials は HTTPS 必須で手元完結の方針に反するため採らない。
		proxy: {
			"/api": {
				target: BACKEND_ORIGIN,
				rewrite: (path) => path.replace(/^\/api/, "")
			}
		}
	}
});
