/**
 * 認証 API（バックエンド T-40 の `routers/auth.py`）。
 *
 * ⚠️ **T-31 が未着手なのでレスポンスの型は手書き**（`GET /config` が未実装で
 * OpenAPI から生成できない）。T-31 に着手したら `openapi-typescript` の生成物へ
 * 寄せること。スキーマの形は `UserResponse`（`user_id` / `email` /
 * `display_name` / `role`）と 1:1 で合わせてある。
 *
 * ⚠️ **トークンを localStorage 等に置かない。** セッションは HttpOnly Cookie
 * （`sid`）で運ばれ、JS からは読めない。フロントは「ログイン済みか」を
 * `GET /auth/me` の成否だけで判断する（T-43 備考）。
 */

import { z } from "zod";
import { apiJson, apiSend, isUnauthorized } from "@/api/client";

/** 権限ロール（バックエンド `enterprise/entities/principal.py` の `Role`）。 */
export const ROLES = ["admin", "editor", "viewer", "system"] as const;

export const roleSchema = z.enum(ROLES);
export type Role = z.infer<typeof roleSchema>;

/** `UserResponse`。⚠️ `password_hash` は API が返さない（返ってきても捨てる）。 */
export const currentUserSchema = z.object({
	user_id: z.string(),
	email: z.string(),
	display_name: z.string(),
	role: roleSchema
});
export type CurrentUser = z.infer<typeof currentUserSchema>;

/**
 * ログイン中の利用者。**未ログインなら `null`**（例外にしない）。
 *
 * ⚠️ 401 は「セッションが無い」という**正常な答え**であって障害ではない。
 * ここで throw すると、未ログインの利用者に毎回エラー表示が出てしまう。
 * 403（権限なし）はそのまま伝播させる（401 と混同しない。T-43 完了条件）。
 */
export async function fetchCurrentUser(): Promise<CurrentUser | null> {
	try {
		return await apiJson("/auth/me", currentUserSchema);
	} catch (error) {
		if (isUnauthorized(error)) {
			return null;
		}
		throw error;
	}
}

export type LoginInput = {
	email: string;
	password: string;
};

/**
 * `POST /auth/login`。成功でサーバーが `sid` Cookie を付ける。
 *
 * 失敗は 401 ＋ **1種類の文言**（「メールアドレスまたはパスワードが違います。」）。
 * ⚠️ **フロントで言い換えないこと。** どちらが違うかを伝えると、そのアドレスが
 * 実在することを教えてしまう（バックエンド `usecases/auth.py` の
 * `LOGIN_FAILED_MESSAGE` と対）。
 */
export async function login(input: LoginInput): Promise<void> {
	await apiSend("/auth/login", { method: "POST", body: input });
}

export type RegisterInput = {
	email: string;
	display_name: string;
	password: string;
};

/**
 * `POST /auth/register`。**作成されるロールは必ず `viewer`**。
 *
 * ⚠️ `role` を送ってはいけない（バックエンドは未知キーを 422 で弾く）。
 * 昇格は admin 限定の `PATCH /users/{id}/role`（T-42）と CLI（T-41）だけ。
 * セッションは発行されないので、登録後は改めてログインする。
 */
export async function register(input: RegisterInput): Promise<CurrentUser> {
	return apiJson("/auth/register", currentUserSchema, {
		method: "POST",
		body: input
	});
}

/** `POST /auth/logout`。べき等（未ログインで叩いても 204）。 */
export async function logout(): Promise<void> {
	await apiSend("/auth/logout", { method: "POST" });
}
