"""生成物の配信（T-27。設計書 §3.3 の `html_url` / `xlsx_url` の実体）。

`GET /files/{filename}`。**全ロールが叩ける**（§6.2 の `GET /reports/{period}`
＝「HTML/一覧」と同じ扱い。一覧を返しながら実体を配らないのは意味を成さない）。

---

⚠️ **`artifact_root` はレポート置き場ではない。同居しているものを見ること。**

```
artifacts/
├── config.json                                   ← admin 限定（§6.1）
├── weekly_ai_intelligence_report.xlsx            ← 配信する
├── monthly_ai_leading_cases.xlsx                 ← 配信する
├── weekly_ai_intelligence_newsletter_不動産_2026-W31.html   ← 配信する
├── monthly_belief_2026-07.html                   ← 配信する
├── raw_articles_2026-W31.json                    ← 配信しない（収集の生データ）
├── validation_2026-W31.json                      ← 配信しない
├── narrative_2026-W31.json                       ← 配信しない
├── _history/                                     ← 配信しない（旧版の退避）
├── _runs/                                        ← 配信しない（ジョブ記録・ロック）
└── scratch/dry-run/{id}/                         ← 配信しない（設計判断C の隔離出力）
```

したがって配信は**許可リスト方式**にしてある（`ArtifactStore.is_servable`）。
「危ないものを弾く」形にすると、新しい種類の成果物が増えるたびに配信経路が
黙って広がる。**特に `config.json` と `scratch/`（ドライランの結果＝未保存の
config を適用した出力）が admin 以外から見えると、§6.1 の
「config は admin 以外に存在も中身も返さない」が配信経路から崩れる。**

⚠️ **`{filename}` は1階層のファイル名だけ。** ディレクトリを辿る記法
（`../` / `a/b`）は許可リストの手前で弾く。Starlette のパスパラメータは
`/` にマッチしないが、それに頼らず値そのものを検証する（`_validate_segment`）。

⚠️ **配信対象外と不在を区別しない（どちらも 404）。** 区別できると、応答の差だけで
`config.json` の有無や scratch の中身を推し量れる（config ルーターと同じ理屈）。
"""

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import FileResponse

from adapter.http.fastapi.auth.dependencies import require_permission
from adapter.storage.artifact_store import ArtifactStore
from config import Settings, get_settings
from enterprise.entities.principal import Principal

logger = logging.getLogger(__name__)

FILES_PREFIX = "/files"

router = APIRouter(prefix=FILES_PREFIX, tags=["files"])

# 拡張子 → Content-Type。⚠️ **配信できるのは HTML と xlsx の2種だけ**なので
# `mimetypes` に委ねない（推測に任せると、許可リストを広げたときに意図しない
# 型で配ることになる）。
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

NOT_FOUND_MESSAGE = "指定された生成物はありません。"


def file_url(filename: str) -> str:
    """`GET /files/{filename}` の URL（`GET /reports` などが返す形）。

    ⚠️ **パーセントエンコードする。** 週刊 HTML の正規名には業界名（日本語）が
    入る（`weekly_..._不動産_2026-W31.html`）。設計書 §3.3 の例は生の日本語だが、
    URL として一意に読める形にしておく（ブラウザはどちらも扱えるが、
    curl やプロキシを挟むと生のマルチバイトは扱いが割れる）。
    """
    return f"{FILES_PREFIX}/{quote(filename)}"


@router.get("/{filename}")
async def get_file(
    filename: Annotated[str, Path(description="生成物のファイル名")],
    _caller: Annotated[Principal, Depends(require_permission)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """生成物（週刊/月刊 HTML・中間xlsx）を返す。**全ロール可**。

    - **200** ファイルの中身
    - **404** 配信対象でない、または実在しない（**両者を区別しない**）
    """
    store = ArtifactStore.from_settings(settings)
    path = store.servable_path(filename)
    if path is None:
        # ⚠️ 何が拒否されたかは**ログにだけ**残す（応答では区別しない）。
        logger.info("配信対象外または不在の生成物への要求: %r", filename)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "file_not_found", "message": NOT_FOUND_MESSAGE},
        )

    return FileResponse(
        path,
        media_type=CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
        filename=path.name,
        content_disposition_type="inline",
    )


__all__ = ["CONTENT_TYPES", "FILES_PREFIX", "file_url", "router"]
