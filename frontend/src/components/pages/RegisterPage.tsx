import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router";
import { z } from "zod";
import { register as registerUser } from "@/api/auth";
import { toDisplayMessage } from "@/api/client";
import { AuthCard, FormError } from "@/components/common/AuthCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LOGIN_PATH } from "@/utils/loginRedirect";

/**
 * パスワードポリシー。バックエンド `enterprise/services/password.py` の
 * `MIN_PASSWORD_LENGTH` / `MAX_PASSWORD_BYTES` と同じ値。
 *
 * ⚠️ **バイト長で見る。** bcrypt の 72 バイト上限は文字数ではなくバイト数で、
 * 日本語は1文字3バイトなので **24文字で上限に達する**。文字数で検査すると
 * サーバー側の 422 で初めて弾かれることになる（サーバーが最終権限であることは
 * 変わらないが、手元で先に気づけるようにする）。
 */
const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_BYTES = 72;

function utf8ByteLength(value: string): number {
	return new TextEncoder().encode(value).length;
}

const registerFormSchema = z
	.object({
		// ⚠️ `role` のフィールドを足さないこと。自己登録は必ず viewer で、
		// バックエンドは未知キーを 422 で弾く（T-40）。
		email: z
			.string()
			.trim()
			.min(1, "メールアドレスを入力してください。")
			// バックエンド `is_valid_email_format()` と同じ緩さ（`local@domain.tld`）。
			// 厳密な RFC 検証はしない（正しいアドレスを誤って弾く事故のほうが多い）。
			.regex(
				/^[^@\s]+@[^@\s]+\.[^@\s]+$/,
				"メールアドレスの形式が正しくありません。"
			),
		display_name: z
			.string()
			.trim()
			.min(1, "表示名を入力してください。")
			.max(128, "表示名は128文字以内にしてください。"),
		password: z
			.string()
			.min(
				MIN_PASSWORD_LENGTH,
				`パスワードは${MIN_PASSWORD_LENGTH}文字以上にしてください。`
			)
			.refine(
				(value) => utf8ByteLength(value) <= MAX_PASSWORD_BYTES,
				`パスワードは UTF-8 で ${MAX_PASSWORD_BYTES} バイト以内にしてください（日本語は1文字3バイト）。`
			),
		password_confirmation: z
			.string()
			.min(1, "確認用のパスワードを入力してください。")
	})
	.refine((values) => values.password === values.password_confirmation, {
		message: "パスワードが一致しません。",
		path: ["password_confirmation"]
	});

type RegisterFormValues = z.infer<typeof registerFormSchema>;

export function RegisterPage() {
	const navigate = useNavigate();

	const form = useForm<RegisterFormValues>({
		resolver: zodResolver(registerFormSchema),
		defaultValues: {
			email: "",
			display_name: "",
			password: "",
			password_confirmation: ""
		}
	});

	const mutation = useMutation({
		mutationFn: registerUser,
		onSuccess: async (user) => {
			// 登録ではセッションを発行しない（T-40）ので、続けてログインしてもらう。
			await navigate(LOGIN_PATH, {
				replace: true,
				state: {
					registeredNotice: `${user.display_name} さんのアカウントを作成しました（権限は閲覧者）。続けてログインしてください。`
				}
			});
		}
	});

	const onSubmit = form.handleSubmit((values) => {
		// ⚠️ `password_confirmation` はサーバーへ送らない（未知キーは 422）。
		mutation.mutate({
			email: values.email,
			display_name: values.display_name,
			password: values.password
		});
	});

	const errors = form.formState.errors;

	return (
		<AuthCard
			title="新規登録"
			description="AI動向把握アプリケーション 管理コンソール"
		>
			{/* T-43 完了条件：ロール選択 UI を置かず、viewer になる旨を画面に明示する。 */}
			<p className="mb-4 rounded-md border bg-muted p-3 text-sm">
				登録したアカウントの権限は<strong>閲覧者（viewer）</strong>
				になります。レポートの閲覧はできますが、
				<strong>ジョブの実行や判断基準（config）の編集はできません</strong>。
				権限が必要な場合は登録後に管理者へ依頼してください。
			</p>

			<FormError
				message={
					mutation.error === null ? null : toDisplayMessage(mutation.error)
				}
			/>

			<form onSubmit={onSubmit} className="grid gap-4" noValidate>
				<div className="grid gap-2">
					<Label htmlFor="register-email">メールアドレス</Label>
					<Input
						id="register-email"
						type="email"
						autoComplete="username"
						aria-invalid={errors.email !== undefined}
						{...form.register("email")}
					/>
					{errors.email !== undefined && (
						<p className="text-sm text-destructive">{errors.email.message}</p>
					)}
				</div>

				<div className="grid gap-2">
					<Label htmlFor="register-display-name">表示名</Label>
					<Input
						id="register-display-name"
						type="text"
						autoComplete="name"
						aria-invalid={errors.display_name !== undefined}
						{...form.register("display_name")}
					/>
					{errors.display_name !== undefined && (
						<p className="text-sm text-destructive">
							{errors.display_name.message}
						</p>
					)}
				</div>

				<div className="grid gap-2">
					<Label htmlFor="register-password">パスワード</Label>
					{/* ⚠️ `type="password"`（マスク表示）を外さないこと。 */}
					<Input
						id="register-password"
						type="password"
						autoComplete="new-password"
						aria-invalid={errors.password !== undefined}
						{...form.register("password")}
					/>
					<p className="text-xs text-muted-foreground">
						{MIN_PASSWORD_LENGTH}文字以上・UTF-8 で {MAX_PASSWORD_BYTES}
						バイト以内。記号の混在は必須ではありません。
					</p>
					{errors.password !== undefined && (
						<p className="text-sm text-destructive">
							{errors.password.message}
						</p>
					)}
				</div>

				<div className="grid gap-2">
					<Label htmlFor="register-password-confirmation">
						パスワード（確認）
					</Label>
					<Input
						id="register-password-confirmation"
						type="password"
						autoComplete="new-password"
						aria-invalid={errors.password_confirmation !== undefined}
						{...form.register("password_confirmation")}
					/>
					{errors.password_confirmation !== undefined && (
						<p className="text-sm text-destructive">
							{errors.password_confirmation.message}
						</p>
					)}
				</div>

				<Button type="submit" disabled={mutation.isPending}>
					{mutation.isPending ? "登録中…" : "登録する"}
				</Button>
			</form>

			<p className="mt-4 text-sm text-muted-foreground">
				既にアカウントがある場合は{" "}
				<Link to={LOGIN_PATH} className="underline">
					ログイン
				</Link>
			</p>
		</AuthCard>
	);
}
