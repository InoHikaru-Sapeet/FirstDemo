"""`config.json` の JSON Schema を `schemas/config.schema.json` へ書き出す。

    make config-schema          # 生成して上書き
    make config-schema-check    # コミット済みファイルが最新かを検査（CI 向け）

**正は Pydantic モデル**（`enterprise.entities.config`）。このスキーマファイルは
生成物であって手で編集しない。外部（フロント・移行 CLI・レビュー）が構造を
参照するための成果物として、リポジトリにコミットしておく。

⚠️ ここは `ArtifactStore`（T-02）を経由しない。ArtifactStore が扱うのは
`artifact_root` 配下の実行時成果物（中間xlsx・生成HTML 等）で、こちらは
開発時に生成してコミットするリポジトリ資産のため置き場が別。
"""

from argparse import ArgumentParser
from pathlib import Path

from enterprise.entities.config import config_json_schema_text

ENCODING = "utf-8"

# backend/src/adapter/cli/export_config_schema.py → backend/schemas/config.schema.json
BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = BACKEND_ROOT / "schemas" / "config.schema.json"


def write_schema(output: Path = DEFAULT_OUTPUT) -> Path:
    """JSON Schema を生成して書き出す。

    Args:
        output: 出力先

    Returns:
        書き出したパス
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(config_json_schema_text(), encoding=ENCODING)
    return output


def is_up_to_date(output: Path = DEFAULT_OUTPUT) -> bool:
    """コミット済みのスキーマがモデルと一致しているか。"""
    if not output.is_file():
        return False
    return output.read_text(encoding=ENCODING) == config_json_schema_text()


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="書き出さず、コミット済みファイルが最新かだけを検査する",
    )
    args = parser.parse_args(argv)

    if args.check:
        if is_up_to_date(args.output):
            print(f"up to date: {args.output}")
            return 0
        print(
            f"stale: {args.output}\n"
            "モデルを変えたら `make config-schema` で生成し直してコミットしてください。"
        )
        return 1

    print(f"wrote: {write_schema(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
