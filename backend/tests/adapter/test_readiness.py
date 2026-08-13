"""レディネスチェック。

既定の DB は SQLite なので、Docker なしで実 DB へ到達できることを確認する。
"""

from fastapi.testclient import TestClient


def test_readyz_reaches_the_database(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
