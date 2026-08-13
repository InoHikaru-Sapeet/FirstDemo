import { afterEach, describe, expect, it, vi } from "vitest";
import { apiJson } from "@/api/client";
import { createQueryClient } from "@/api/query-client";
import { authKeys } from "@/api/query-keys";
import { errorResponse, stubFetch } from "@/test/http";

const ADMIN = {
	user_id: "usr_admin",
	email: "admin@sapeet.com",
	display_name: "運用担当",
	role: "admin"
};

const passthrough = { parse: (data: unknown): unknown => data };

async function fetchAndSwallow(client: ReturnType<typeof createQueryClient>) {
	await client
		.fetchQuery({
			queryKey: ["config"],
			queryFn: () => apiJson("/config", passthrough),
			retry: false
		})
		.catch(() => undefined);
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("createQueryClient", () => {
	it("401 を受けたら ['auth','me'] を null にする（セッション失効でログイン画面へ落ちる）", async () => {
		stubFetch({
			"/api/config": () => errorResponse(401, "ログインが必要です。")
		});
		const client = createQueryClient();
		client.setQueryData(authKeys.me(), ADMIN);

		await fetchAndSwallow(client);

		expect(client.getQueryData(authKeys.me())).toBeNull();
	});

	it("403 ではログイン状態を落とさない（権限なしは再ログインで解決しない）", async () => {
		stubFetch({
			"/api/config": () => errorResponse(403, { message: "権限がありません。" })
		});
		const client = createQueryClient();
		client.setQueryData(authKeys.me(), ADMIN);

		await fetchAndSwallow(client);

		expect(client.getQueryData(authKeys.me())).toEqual(ADMIN);
	});
});
