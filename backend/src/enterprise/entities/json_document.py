"""JSON 成果物の読み書き。**壊れたファイルはパス付きのエラーで落とす。**

`raw_articles_{period}.json`（設計書 §2.3）と `validation_{period}.json`（§2.4）は
crawl → filter → render のステップ間受け渡し単位（§8.2）。壊れたファイルを黙って
通すと、欠落した記事や検証結果が「無かったこと」になって最終成果物に紛れ込む。
各ステップは独立に再実行される前提（§14）なので、読み込み時に気づけないと
どこで落ちたのか追えなくなる。

そのため **パースは常に全件検証** し、失敗したら **どの要素のどのフィールドが
なぜダメか** を含む例外で落とす。壊れた要素だけ読み飛ばす、という挙動は持たせない。

`path` は T-05 の `ConfigIssue.path` と同じドット区切り（`3.url` = 4番目の記事の
`url`）。Pydantic の `ValidationError.loc` をそのまま繋いだ形。
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

ENCODING = "utf-8"

# JSON の loc が空（ルート自体が不正）のときの表示。
ROOT_PATH = "(root)"


class DocumentIssue(BaseModel):
    """「どの要素のどのフィールドがなぜダメか」1件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    reason: str


class DocumentParseError(Exception):
    """JSON 成果物が読めない／スキーマに合わない。

    Attributes:
        label: どのファイルか（`raw_articles.json` 等）
        issues: 違反の一覧。全件返すので1回の実行で全部直せる
    """

    def __init__(self, label: str, issues: list[DocumentIssue]) -> None:
        self.label = label
        self.issues = issues
        detail = "; ".join(f"{issue.path}: {issue.reason}" for issue in issues)
        super().__init__(f"{label} を読み込めません — {detail}")


def parse_json_document[T](adapter: TypeAdapter[T], text: str, *, label: str) -> T:
    """JSON テキストを検証しながら読み込む。

    Args:
        adapter: 対象の型アダプタ（`TypeAdapter(list[RawArticle])` 等）
        text: JSON テキスト（UTF-8 で読んだもの）
        label: エラーメッセージに出すファイル名

    Returns:
        検証を通ったオブジェクト

    Raises:
        DocumentParseError: JSON として壊れている、またはスキーマに合わない場合
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        issue = DocumentIssue(
            path=f"line {exc.lineno} column {exc.colno}", reason=exc.msg
        )
        raise DocumentParseError(label, [issue]) from exc

    return validate_json_data(adapter, data, label=label)


def validate_json_data[T](adapter: TypeAdapter[T], data: Any, *, label: str) -> T:
    """すでに `json.loads` 済みのデータを検証する。

    Raises:
        DocumentParseError: スキーマに合わない場合
    """
    try:
        return adapter.validate_python(data)
    except ValidationError as exc:
        raise DocumentParseError(label, _issues_from(exc)) from exc


def dump_json_document[T](adapter: TypeAdapter[T], value: T) -> str:
    """JSON テキストへ書き出す。

    日本語（`region_hint` 等）をエスケープせず、末尾に改行を付ける。
    入出力はすべて UTF-8（設計書 §14）で、人が diff を読める形にしておく。
    """
    payload = adapter.dump_python(value, mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _issues_from(exc: ValidationError) -> list[DocumentIssue]:
    return [
        DocumentIssue(
            path=".".join(str(part) for part in error["loc"]) or ROOT_PATH,
            reason=error["msg"],
        )
        for error in exc.errors()
    ]
