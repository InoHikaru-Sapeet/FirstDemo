import { vi } from "vitest";

/** テストが `fetch` を差し替えるための道具（T-43）。 */

export type StubbedRequest = {
	url: string;
	method: string;
	credentials: RequestCredentials | undefined;
	body: unknown;
};

export type RouteHandler = (
	request: StubbedRequest
) => Response | Promise<Response>;

/** ルートのキーは `"POST /api/auth/login"` または `"/api/auth/me"`（メソッド省略）。 */
export type Routes = Record<string, RouteHandler>;

export function jsonResponse(data: unknown, status = 200): Response {
	return new Response(JSON.stringify(data), {
		status,
		headers: { "Content-Type": "application/json" }
	});
}

export function noContentResponse(): Response {
	return new Response(null, { status: 204 });
}

/** バックエンドの `HTTPException(detail={"error":..., "message":...})` 相当。 */
export function errorResponse(status: number, detail: unknown): Response {
	return jsonResponse({ detail }, status);
}

export function stubFetch(routes: Routes) {
	const requests: StubbedRequest[] = [];

	const fetchMock = vi.fn(
		async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = typeof input === "string" ? input : String(input);
			const method = init?.method ?? "GET";

			let body: unknown;
			if (typeof init?.body === "string") {
				body = JSON.parse(init.body);
			}

			const request: StubbedRequest = {
				url,
				method,
				credentials: init?.credentials,
				body
			};
			requests.push(request);

			const handler = routes[`${method} ${url}`] ?? routes[url];
			if (handler === undefined) {
				// 想定外のリクエストは 501 で返し、原因が画面／例外に出るようにする。
				return errorResponse(501, {
					message: `stubFetch: ルート未登録 ${method} ${url}`
				});
			}
			return handler(request);
		}
	);

	vi.stubGlobal("fetch", fetchMock);
	return { requests, fetchMock };
}
