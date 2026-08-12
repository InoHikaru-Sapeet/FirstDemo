FROM python:3.13-slim
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 依存だけ先に入れてレイヤキャッシュを効かせる
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    uv pip install --system --no-config -r /tmp/requirements.txt

COPY src/ ./src/
ENV PYTHONPATH=/app/src

# 非 root ユーザーで動かす
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "adapter.http.fastapi.main:app", "--host", "0.0.0.0", "--port", "8000"]
