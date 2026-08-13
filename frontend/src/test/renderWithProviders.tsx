import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { createQueryClient } from "@/api/query-client";

type InitialEntry = string | { pathname: string; state?: unknown };

type Options = {
	initialEntries?: InitialEntry[];
	queryClient?: QueryClient;
};

/**
 * アプリと同じ Provider（TanStack Query / react-router）の下で描画する。
 *
 * `QueryClient` は**テストごとに作る**（アプリのシングルトンを使うと、
 * `['auth','me']` のキャッシュが次のテストへ漏れる）。
 */
export function renderWithProviders(ui: ReactNode, options: Options = {}) {
	const queryClient = options.queryClient ?? createQueryClient();
	const initialEntries = options.initialEntries ?? ["/"];

	const result = render(
		<QueryClientProvider client={queryClient}>
			<MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
		</QueryClientProvider>
	);

	return { ...result, queryClient };
}
