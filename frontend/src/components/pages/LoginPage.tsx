import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, Navigate, useLocation, useNavigate } from "react-router";
import { z } from "zod";
import { fetchCurrentUser, login } from "@/api/auth";
import { toDisplayMessage } from "@/api/client";
import { authKeys } from "@/api/query-keys";
import { AuthCard, FormError } from "@/components/common/AuthCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { REGISTER_PATH, readLoginRedirectTarget } from "@/utils/loginRedirect";

/**
 * ⚠️ **メール形式の検査をここでしない。** ログインの失敗理由はサーバー側で
 * 1種類に統一されている（どちらが違うかを明かさない）。フロントで
 * 「メールアドレスの形式が違います」を出すと、入力のどこが問題かを段階的に
 * 教えることになり、統一の意図が薄れる。空欄だけを弾く。
 */
const loginFormSchema = z.object({
	email: z.string().trim().min(1, "メールアドレスを入力してください。"),
	password: z.string().min(1, "パスワードを入力してください。")
});

type LoginFormValues = z.infer<typeof loginFormSchema>;

/** 登録画面から遷移してきたときのお知らせ（`RegisterPage` が state で渡す）。 */
function readRegisteredNotice(state: unknown): string | null {
	if (
		typeof state === "object" &&
		state !== null &&
		"registeredNotice" in state &&
		typeof state.registeredNotice === "string"
	) {
		return state.registeredNotice;
	}
	return null;
}

export function LoginPage() {
	const location = useLocation();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const { isAuthenticated } = useCurrentUser();

	const redirectTo = readLoginRedirectTarget(location.state);
	const registeredNotice = readRegisteredNotice(location.state);

	const form = useForm<LoginFormValues>({
		resolver: zodResolver(loginFormSchema),
		defaultValues: { email: "", password: "" }
	});

	const mutation = useMutation({
		mutationFn: login,
		onSuccess: async () => {
			// ⚠️ 遷移の前に `GET /auth/me` を取り直す。`['auth','me']` に古い `null` が
			// 残ったまま認証必須ルートへ行くと `RequireAuth` に押し戻される。
			const user = await fetchCurrentUser();
			queryClient.setQueryData(authKeys.me(), user);
			await navigate(redirectTo, { replace: true });
		}
	});

	// 既にログイン済みならログイン画面を見せる意味がない。
	if (isAuthenticated && !mutation.isPending) {
		return <Navigate to={redirectTo} replace />;
	}

	const onSubmit = form.handleSubmit((values) => {
		mutation.mutate({ email: values.email, password: values.password });
	});

	return (
		<AuthCard
			title="ログイン"
			description="AI動向把握アプリケーション 管理コンソール"
		>
			{registeredNotice !== null && (
				<p className="mb-4 rounded-md border bg-muted p-3 text-sm">
					{registeredNotice}
				</p>
			)}

			{/* サーバーの文言をそのまま出す（言い換えない）。 */}
			<FormError
				message={
					mutation.error === null ? null : toDisplayMessage(mutation.error)
				}
			/>

			<form onSubmit={onSubmit} className="grid gap-4" noValidate>
				<div className="grid gap-2">
					<Label htmlFor="login-email">メールアドレス</Label>
					<Input
						id="login-email"
						type="email"
						autoComplete="username"
						aria-invalid={form.formState.errors.email !== undefined}
						{...form.register("email")}
					/>
					{form.formState.errors.email !== undefined && (
						<p className="text-sm text-destructive">
							{form.formState.errors.email.message}
						</p>
					)}
				</div>

				<div className="grid gap-2">
					<Label htmlFor="login-password">パスワード</Label>
					{/* ⚠️ `type="password"`（マスク表示）を外さないこと。 */}
					<Input
						id="login-password"
						type="password"
						autoComplete="current-password"
						aria-invalid={form.formState.errors.password !== undefined}
						{...form.register("password")}
					/>
					{form.formState.errors.password !== undefined && (
						<p className="text-sm text-destructive">
							{form.formState.errors.password.message}
						</p>
					)}
				</div>

				<Button type="submit" disabled={mutation.isPending}>
					{mutation.isPending ? "ログイン中…" : "ログイン"}
				</Button>
			</form>

			<p className="mt-4 text-sm text-muted-foreground">
				アカウントがない場合は{" "}
				<Link to={REGISTER_PATH} className="underline">
					新規登録
				</Link>
			</p>
		</AuthCard>
	);
}
