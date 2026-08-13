"""enterprise 層テストの共通フィクスチャ。

基準となる入力は仕様書 §5.2 の確定 config を逐語でコピーした
`data/config_initial.json`。設計書末尾の指示どおり、モデル（T-04）と
クロスフィールドバリデータ（T-05）はどちらもこの実データを基準にする。
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from enterprise.entities.config import IntelligenceConfig

INITIAL_CONFIG_PATH = Path(__file__).parent / "data" / "config_initial.json"


@pytest.fixture(scope="session")
def initial_raw() -> dict[str, Any]:
    """仕様書 §5.2 の確定 config（xlsx 実データより生成された初期値）。

    session スコープで読み込みは1回だけ。壊してよいコピーが要るテストは
    `raw` を使う。
    """
    return json.loads(INITIAL_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def raw(initial_raw: dict[str, Any]) -> dict[str, Any]:
    """テストごとに壊してよい生 JSON のコピー。"""
    return copy.deepcopy(initial_raw)


@pytest.fixture
def config(raw: dict[str, Any]) -> IntelligenceConfig:
    """§5.2 の確定 config をモデルへ通したもの。テストごとに使い捨て。"""
    return IntelligenceConfig.model_validate(raw)
