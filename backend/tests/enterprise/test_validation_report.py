"""`validation_*.json` のスキーマ（設計書 §2.4 ／ 仕様書 §12.2）。

`ok` が「error が無いこと」である前提を固定する。ここが緩いと、不備のある記事が
本編HTML へ紛れ込む（§12.2「エラーがある記事は本編HTML生成の対象から除外」）。
"""

import json
from typing import Any

import pytest

from enterprise.entities.json_document import DocumentParseError
from enterprise.entities.validation_report import (
    VALIDATION_REPORT_ADAPTER,
    ValidationIssue,
    ValidationReport,
    dump_validation_report,
    parse_validation_report,
)


def _paths(exc_info: pytest.ExceptionInfo[DocumentParseError]) -> list[str]:
    return [issue.path for issue in exc_info.value.issues]


@pytest.fixture
def report_payload() -> dict[str, Any]:
    """§12.2 の error / warning 例を1件ずつ持つレポート。"""
    return {
        "ok": False,
        "errors": [
            {"row": 7, "field": "合計スコア", "reason": "6軸点の和(85)と一致しない(87)"}
        ],
        "warnings": [{"row": 9, "field": "一言要約", "reason": "要約が短すぎる"}],
    }


# --- 正常系 ------------------------------------------------------------------


def test_report_from_the_design_example_is_accepted(
    report_payload: dict[str, Any],
) -> None:
    report = parse_validation_report(json.dumps(report_payload, ensure_ascii=False))

    assert report.ok is False
    assert len(report.errors) == 1
    assert report.errors[0].row == 7
    assert report.errors[0].field == "合計スコア"
    assert report.warnings[0].reason == "要約が短すぎる"


def test_clean_report_is_ok() -> None:
    report = parse_validation_report('{"ok": true, "errors": [], "warnings": []}')

    assert report.ok is True
    assert report.errors == []
    assert report.warnings == []


def test_report_round_trips(report_payload: dict[str, Any]) -> None:
    report = parse_validation_report(json.dumps(report_payload, ensure_ascii=False))

    text = dump_validation_report(report)

    assert json.loads(text) == report_payload
    assert parse_validation_report(text) == report


def test_dump_keeps_japanese_readable_and_ends_with_a_newline(
    report_payload: dict[str, Any],
) -> None:
    """入出力は UTF-8（設計書 §14）。"""
    report = parse_validation_report(json.dumps(report_payload, ensure_ascii=False))

    text = dump_validation_report(report)

    assert "一言要約" in text
    assert "\\u" not in text
    assert text.endswith("\n")


def test_dump_preserves_the_field_order_from_the_design(
    report_payload: dict[str, Any],
) -> None:
    report = parse_validation_report(json.dumps(report_payload, ensure_ascii=False))

    body = json.loads(dump_validation_report(report))

    assert list(body) == ["ok", "errors", "warnings"]
    assert list(body["errors"][0]) == ["row", "field", "reason"]


# --- ok と errors の整合（§12.2）--------------------------------------------


def test_from_issues_computes_ok_from_the_errors() -> None:
    """生成側（T-20）の入口。`ok` を手で立てなくて済む。"""
    clean = ValidationReport.from_issues()
    assert clean.ok is True

    warned = ValidationReport.from_issues(
        warnings=[ValidationIssue(row=9, field="一言要約", reason="短すぎる")]
    )
    assert warned.ok is True

    failed = ValidationReport.from_issues(
        errors=[ValidationIssue(row=7, field="合計スコア", reason="不一致")]
    )
    assert failed.ok is False


def test_warnings_alone_do_not_make_the_report_not_ok() -> None:
    """warning の記事は本編に残る（§12.2）。"""
    payload = {
        "ok": True,
        "errors": [],
        "warnings": [{"row": 9, "field": "一言要約", "reason": "短すぎる"}],
    }

    assert parse_validation_report(json.dumps(payload, ensure_ascii=False)).ok is True


def test_ok_true_with_errors_is_rejected() -> None:
    """⚠️ これを通すと不備のある記事が本編HTMLへ載ってしまう（§12.2）。"""
    payload = {
        "ok": True,
        "errors": [{"row": 7, "field": "合計スコア", "reason": "不一致"}],
        "warnings": [],
    }

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload, ensure_ascii=False))

    assert "矛盾" in exc_info.value.issues[0].reason


def test_ok_false_without_errors_is_rejected() -> None:
    """逆向きの取り違えも弾く（採用できる記事を落としてしまう）。"""
    payload = {"ok": False, "errors": [], "warnings": []}

    with pytest.raises(DocumentParseError):
        parse_validation_report(json.dumps(payload))


def test_constructing_an_inconsistent_report_in_code_also_fails() -> None:
    with pytest.raises(ValueError, match="矛盾"):
        ValidationReport(
            ok=True,
            errors=[ValidationIssue(row=1, field="URL", reason="空")],
            warnings=[],
        )


# --- 必須・未知キー ----------------------------------------------------------


@pytest.mark.parametrize("key", ["ok", "errors", "warnings"])
def test_top_level_keys_are_all_required(key: str) -> None:
    """設計書 §2.4 の `required` は3つとも。"""
    payload: dict[str, Any] = {"ok": True, "errors": [], "warnings": []}
    del payload[key]

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload))

    assert _paths(exc_info) == [key]


@pytest.mark.parametrize("key", ["row", "field", "reason"])
def test_issue_keys_are_all_required(key: str) -> None:
    """設計書 §2.4 の `$defs/issue` の `required`。"""
    issue: dict[str, Any] = {"row": 7, "field": "合計スコア", "reason": "不一致"}
    del issue[key]
    payload = {"ok": False, "errors": [issue], "warnings": []}

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload, ensure_ascii=False))

    assert _paths(exc_info) == [f"errors.0.{key}"]


def test_unknown_keys_are_rejected() -> None:
    """`additionalProperties: false`（設計書 §2.4）。"""
    payload = {"ok": True, "errors": [], "warnings": [], "summary": "全件OK"}

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload, ensure_ascii=False))

    assert _paths(exc_info) == ["summary"]


def test_unknown_keys_in_an_issue_are_rejected() -> None:
    payload = {
        "ok": False,
        "errors": [{"row": 7, "field": "URL", "reason": "空", "severity": "high"}],
        "warnings": [],
    }

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload, ensure_ascii=False))

    assert _paths(exc_info) == ["errors.0.severity"]


def test_non_integer_row_is_rejected() -> None:
    payload = {
        "ok": False,
        "errors": [{"row": "七行目", "field": "URL", "reason": "空"}],
        "warnings": [],
    }

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload, ensure_ascii=False))

    assert _paths(exc_info) == ["errors.0.row"]


# --- 壊れた JSON はパス付きで落ちる ------------------------------------------


def test_malformed_json_reports_where_it_broke() -> None:
    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report('{"ok": true, "errors": [},')

    assert exc_info.value.label == "validation.json"
    assert "line" in exc_info.value.issues[0].path


def test_non_object_payload_is_rejected_at_the_root() -> None:
    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report("[]")

    assert _paths(exc_info) == ["(root)"]


def test_every_problem_is_reported_not_just_the_first() -> None:
    payload = {
        "ok": False,
        "errors": [{"row": "x", "field": "URL"}],
        "warnings": [{"row": 1}],
    }

    with pytest.raises(DocumentParseError) as exc_info:
        parse_validation_report(json.dumps(payload, ensure_ascii=False))

    assert set(_paths(exc_info)) >= {
        "errors.0.row",
        "errors.0.reason",
        "warnings.0.field",
        "warnings.0.reason",
    }


# --- 生成 JSON Schema が設計書 §2.4 と揃っていること -------------------------


def test_json_schema_matches_the_design() -> None:
    schema = VALIDATION_REPORT_ADAPTER.json_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["ok", "errors", "warnings"]
    assert schema["properties"]["ok"]["type"] == "boolean"
    assert schema["properties"]["errors"]["type"] == "array"
    assert schema["properties"]["warnings"]["type"] == "array"

    issue = schema["$defs"]["ValidationIssue"]
    assert issue["additionalProperties"] is False
    assert issue["required"] == ["row", "field", "reason"]
    assert issue["properties"]["row"]["type"] == "integer"
    assert issue["properties"]["field"]["type"] == "string"
    assert issue["properties"]["reason"]["type"] == "string"
