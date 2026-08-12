import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "./App";

describe("App", () => {
	it("ナビゲーションのリンクが表示される", () => {
		render(<App />);
		expect(screen.getAllByText("レポート一覧").length).toBeGreaterThan(0);
		expect(screen.getByText("判断基準（管理者）")).toBeInTheDocument();
	});
});
