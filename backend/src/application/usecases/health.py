"""ヘルスチェックのユースケース。

adapter（HTTP）→ application（ユースケース）の最小の流れを示すための雛形。
実ロジックを足す時はこの層に置き、adapter は薄く保つ。
"""


class HealthUsecase:
    def check(self) -> dict[str, str]:
        return {"status": "ok"}
