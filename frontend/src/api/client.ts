/**
 * fetch ラッパ（T-43 で必要な最小限）。
 *
 * ⚠️ **T-31（API 型生成とクライアント基盤）はまだ未着手**で、依存する `GET /config`
 * （T-12）・`POST /run`（T-27）が実装されていないため OpenAPI から型を生成できない。
 * T-43 は認証画面のためにクライアントが必要なので、T-31 の完了条件のうち
 * **今使う分だけ**をここに先取りしてある：
 *
 * - すべてのリクエストに `credentials: "include"` を付ける
 * - 401 / 403 / 409 / 422 を区別できる形で返す（401 と 403 を混同しない）
 *
 * T-31 に着手したら、レスポンスの型は手書き zod スキーマ（`api/auth.ts`）から
 * `openapi-typescript` の生成物へ寄せること。**この層の 401/403 の扱いは変えない**。
 */

/** dev では Vite の proxy がここを `http://localhost:8000` へ転送する（vite.config.ts）。 */
export const API_BASE_PATH = "/api";

/** ネットワーク到達不能。HTTP ステータスが無いことを表す番兵。 */
export const NETWORK_ERROR_STATUS = 0;

/** 422 の `issues[]` 1件（バックエンドの `PasswordIssue` / `ConfigIssue` に対応）。 */
export type ApiIssue = {
	code: string | null;
	reason: string | null;
	path: string | null;
};

/**
 * API が非 2xx を返した／到達できなかった。
 *
 * `message` は**利用者にそのまま見せられる文言**。バックエンドが返した文言を
 * 優先して入れる（ログイン失敗の「メールアドレスまたはパスワードが違います。」を
 * フロントで言い換えないため。T-43 完了条件）。
 */
export class ApiError extends Error {
	readonly status: number;
	readonly code: string | null;
	readonly issues: readonly ApiIssue[];

	constructor(
		status: number,
		message: string,
		code: string | null,
		issues: readonly ApiIssue[],
		options?: ErrorOptions
	) {
		super(message, options);
		this.name = "ApiError";
		this.status = status;
		this.code = code;
		this.issues = issues;
	}
}

export function isApiError(error: unknown): error is ApiError {
	return error instanceof ApiError;
}

/**
 * 401 = **未ログイン**（セッションが無い・失効した）。
 *
 * ⚠️ 403（ログイン済みだが権限が無い）と混同しないこと。401 はログイン画面へ
 * 誘導するが、403 は画面内で「権限がありません」を出すだけで、ログイン画面へ
 * 飛ばしてはいけない（再ログインしても解決しないため）。
 */
export function isUnauthorized(error: unknown): boolean {
	return isApiError(error) && error.status === 401;
}

/** 403 = ログイン済みだが権限が無い。画面内で処理する。 */
export function isForbidden(error: unknown): boolean {
	return isApiError(error) && error.status === 403;
}

/** 409 = 競合（登録済みメール・revision 競合）。 */
export function isConflict(error: unknown): boolean {
	return isApiError(error) && error.status === 409;
}

/** 422 = 入力の検証エラー。`issues[]` を持つことがある。 */
export function isValidationError(error: unknown): boolean {
	return isApiError(error) && error.status === 422;
}

/** 例外から利用者向けの文言を取り出す（想定外の例外にも文言を出せるように）。 */
export function toDisplayMessage(error: unknown): string {
	if (isApiError(error)) {
		return error.message;
	}
	return "予期しないエラーが発生しました。";
}

/** JSON を検証してから型を付けるための最小インタフェース（zod スキーマが満たす）。 */
type ResponseParser<T> = {
	parse: (data: unknown) => T;
};

type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export type RequestOptions = {
	method?: HttpMethod;
	body?: unknown;
	signal?: AbortSignal;
};

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
	return typeof value === "string" && value !== "" ? value : null;
}

function extractIssues(detail: unknown): ApiIssue[] {
	if (!isRecord(detail) || !Array.isArray(detail.issues)) {
		return [];
	}

	const issues: ApiIssue[] = [];
	for (const entry of detail.issues) {
		if (!isRecord(entry)) {
			continue;
		}
		issues.push({
			code: stringOrNull(entry.code),
			reason: stringOrNull(entry.reason),
			path: stringOrNull(entry.path)
		});
	}
	return issues;
}

function fallbackMessage(status: number): string {
	switch (status) {
		case NETWORK_ERROR_STATUS:
			return "サーバーに接続できませんでした。通信環境を確認してください。";
		case 401:
			return "ログインが必要です。";
		case 403:
			return "この操作を行う権限がありません。";
		case 409:
			return "他の更新と競合しました。読み込み直してください。";
		case 422:
			return "入力内容を確認してください。";
		default:
			return `サーバーとの通信に失敗しました。（HTTP ${status}）`;
	}
}

/**
 * エラーボディから文言を取り出す。
 *
 * バックエンドの形が2種類あるため両方を見る:
 * - 自前の `HTTPException`：`{detail: {error, message}}` / `{detail: {error, issues}}`
 * - FastAPI の入力検証：`{detail: [{loc, msg, type}]}`
 */
function extractMessage(detail: unknown, status: number): string {
	const direct = stringOrNull(detail);
	if (direct !== null) {
		return direct;
	}

	if (isRecord(detail)) {
		const message = stringOrNull(detail.message);
		if (message !== null) {
			return message;
		}
	}

	const reasons: string[] = [];
	for (const issue of extractIssues(detail)) {
		if (issue.reason !== null) {
			reasons.push(issue.reason);
		}
	}
	if (reasons.length > 0) {
		return reasons.join(" ");
	}

	if (Array.isArray(detail)) {
		const messages: string[] = [];
		for (const entry of detail) {
			const msg = isRecord(entry) ? stringOrNull(entry.msg) : null;
			if (msg !== null) {
				messages.push(msg);
			}
		}
		if (messages.length > 0) {
			return messages.join(" ");
		}
	}

	return fallbackMessage(status);
}

async function toApiError(response: Response): Promise<ApiError> {
	let detail: unknown;
	try {
		const body: unknown = await response.json();
		detail = isRecord(body) && "detail" in body ? body.detail : body;
	} catch {
		// ボディが JSON でない（プロキシのエラーページ等）。ステータスだけで判断する。
		detail = undefined;
	}

	const code = isRecord(detail) ? stringOrNull(detail.error) : null;
	return new ApiError(
		response.status,
		extractMessage(detail, response.status),
		code,
		extractIssues(detail)
	);
}

async function request(
	path: string,
	options: RequestOptions
): Promise<Response> {
	const { method = "GET", body, signal } = options;
	const headers: Record<string, string> = { Accept: "application/json" };
	if (body !== undefined) {
		headers["Content-Type"] = "application/json";
	}

	let response: Response;
	try {
		response = await fetch(`${API_BASE_PATH}${path}`, {
			method,
			headers,
			// ⚠️ 認証は HttpOnly Cookie（`sid`）。**JS 側でトークンを保持しない**ので、
			// これが無いと Cookie が送られず全リクエストが 401 になる。
			credentials: "include",
			body: body === undefined ? undefined : JSON.stringify(body),
			signal
		});
	} catch (cause) {
		throw new ApiError(
			NETWORK_ERROR_STATUS,
			fallbackMessage(NETWORK_ERROR_STATUS),
			null,
			[],
			{ cause }
		);
	}

	if (!response.ok) {
		throw await toApiError(response);
	}
	return response;
}

/**
 * JSON を返す API を呼ぶ。
 *
 * `as` キャストを避けるため、レスポンスは必ず `parser`（zod スキーマ）を通す
 * （`frontend/CLAUDE.md` の規約）。
 */
export async function apiJson<T>(
	path: string,
	parser: ResponseParser<T>,
	options: RequestOptions = {}
): Promise<T> {
	const response = await request(path, options);
	const data: unknown = await response.json();
	return parser.parse(data);
}

/** ボディを読まない API を呼ぶ（204、または戻り値に意味が無い 200）。 */
export async function apiSend(
	path: string,
	options: RequestOptions = {}
): Promise<void> {
	await request(path, options);
}
