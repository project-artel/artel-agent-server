FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080

WORKDIR /app

COPY pyproject.toml README.md ./

FROM base AS test

COPY app ./app
COPY tests ./tests

RUN pip install --no-cache-dir -e ".[dev]"
RUN python -m pytest

FROM base AS runtime

COPY app ./app

RUN pip install --no-cache-dir .

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]


{"step1", "~~해라"} //
{step_id, func, arg}
..
..

