FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app

# tzdata — чтобы время событий в логе можно было увести из UTC в нужный пояс
# через переменную TZ (например, -e TZ=Europe/Moscow).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Зависимости отдельным слоем: пересобираются только при изменении lock-файла.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

COPY main.py ./

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 4318

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4318"]
