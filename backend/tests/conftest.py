"""テスト共通フィクスチャ。"""

import pytest
from fastapi.testclient import TestClient

from adapter.http.fastapi.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
