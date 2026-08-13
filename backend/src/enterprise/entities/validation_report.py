"""`validation_{period}.json` のスキーマ（設計書 §2.4 ／ 仕様書 §12.2）。

フォーマットチェック（T-20）の出力。週次フィルタリング完了時に自動実行され、
スコアリング根拠の記載漏れを検知する（§12）。

`error` と `warning` の切り分けは §12.2 の確定事項:

- `error`: 合計スコア不一致・enum 外の値・必須タグ欠落。**該当記事は本編HTML生成の
  対象から外し、除外ログに `除外区分=フォーマット不備` として記録する**
- `warning`: 要約が短すぎる等。記事は本編に残る

したがって **`ok` は「error が無いこと」**。warning は `ok` を左右しない。
"""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from enterprise.entities.json_document import (
    dump_json_document,
    parse_json_document,
)

VALIDATION_REPORT_LABEL = "validation.json"


class ValidationIssue(BaseModel):
    """検証で見つかった1件（設計書 §2.4 の `$defs/issue`）。"""

    model_config = ConfigDict(extra="forbid")

    row: int
    """対象 xlsx 行（1-indexed）。週次のデータ行は5行目から（T-07）。"""

    field: str
    """列名またはタグID（`合計スコア` / `adoption_class` 等）。"""

    reason: str


class ValidationReport(BaseModel):
    """検証レポート全体（設計書 §2.4）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]

    @model_validator(mode="after")
    def _ok_must_agree_with_errors(self) -> "ValidationReport":
        """`ok` と `errors` の食い違いを拒否する。

        ⚠️ この整合性は設計書 §2.4 のスキーマには書かれていない（`ok` は素の
        boolean）。ただし §12.2 は「エラーがある記事は本編HTML生成の対象から
        除外する」と定めており、`ok=true` なのに `errors` が入ったレポートを
        通すと **不備のある記事が本編に載ってしまう**。黙って通す方が危ないので
        ここで弾く。生成側は `from_issues()` を使えば取り違えない。
        """
        if self.ok != (not self.errors):
            raise ValueError(
                f"ok={self.ok} と errors {len(self.errors)}件 が矛盾しています"
                "（ok は「error が無いこと」。§12.2）"
            )
        return self

    @classmethod
    def from_issues(
        cls,
        *,
        errors: Sequence[ValidationIssue] = (),
        warnings: Sequence[ValidationIssue] = (),
    ) -> "ValidationReport":
        """`ok` を errors から決めてレポートを組み立てる（生成側の入口）。

        Args:
            errors: 本編から外すべき不備
            warnings: 記事は残すが直したい点

        Returns:
            `ok = errors が空` のレポート
        """
        return cls(ok=not errors, errors=list(errors), warnings=list(warnings))


VALIDATION_REPORT_ADAPTER: TypeAdapter[ValidationReport] = TypeAdapter(ValidationReport)


def parse_validation_report(text: str) -> ValidationReport:
    """`validation_{period}.json` を読み込む。

    Args:
        text: JSON テキスト（ArtifactStore 経由で UTF-8 読み込み）

    Returns:
        検証レポート

    Raises:
        DocumentParseError: JSON が壊れている、またはスキーマに合わない場合。
            どのフィールドがなぜダメかを含む
    """
    return parse_json_document(
        VALIDATION_REPORT_ADAPTER, text, label=VALIDATION_REPORT_LABEL
    )


def dump_validation_report(report: ValidationReport) -> str:
    """`validation_{period}.json` として書き出す文字列。"""
    return dump_json_document(VALIDATION_REPORT_ADAPTER, report)
