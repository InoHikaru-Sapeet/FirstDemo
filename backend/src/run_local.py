"""ローカル開発用の起動スクリプト。`uv run src/run_local.py` で立ち上がる。"""

from argparse import ArgumentParser

import uvicorn

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    uvicorn.run(
        "adapter.http.fastapi.main:app",
        host=args.host,
        port=args.port,
        reload=True,
    )
