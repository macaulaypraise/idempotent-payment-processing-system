# ── Stage 1: builder ──────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install poetry

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.in-project true && \
    poetry install --no-root --only main

# ── Stage 2: runtime ──────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv .venv

COPY . .

EXPOSE 8000

CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
