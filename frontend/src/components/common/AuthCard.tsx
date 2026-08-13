import type { ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle
} from "@/components/ui/card";

type AuthCardProps = {
	title: string;
	description?: ReactNode;
	children: ReactNode;
};

/** ログイン／登録画面の共通の枠。ログイン不要なページなのでナビを出さない。 */
export function AuthCard({ title, description, children }: AuthCardProps) {
	return (
		<main className="flex min-h-screen items-center justify-center p-6">
			<Card className="w-full max-w-md">
				<CardHeader>
					{/* shadcn の CardTitle は `div` なので、見出しの役割は h1 に持たせる。
					    Tailwind の preflight が h1 の既定サイズ・余白を消すため見た目は変わらない。 */}
					<CardTitle className="text-lg">
						<h1>{title}</h1>
					</CardTitle>
					{description !== undefined && (
						<CardDescription>{description}</CardDescription>
					)}
				</CardHeader>
				<CardContent>{children}</CardContent>
			</Card>
		</main>
	);
}

type FormErrorProps = {
	message: string | null;
};

/**
 * サーバー／検証エラーの表示。
 *
 * ⚠️ ログイン失敗の文言は**サーバーが返したものをそのまま出す**。
 * 「このメールは未登録です」等に言い換えるとアカウントの列挙を許してしまう
 * （T-43 完了条件・バックエンドの `LOGIN_FAILED_MESSAGE` と対）。
 */
export function FormError({ message }: FormErrorProps) {
	if (message === null) {
		return null;
	}

	return (
		<Alert variant="destructive" className="mb-4">
			<AlertDescription>{message}</AlertDescription>
		</Alert>
	);
}
