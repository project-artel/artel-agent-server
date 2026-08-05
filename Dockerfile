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

# What build this is. A QA run records these so a past agent structure can be
# reproduced by redeploying its image — old structures are identified, not kept
# as parallel copies in the source tree. Declared last so a new sha does not
# invalidate the dependency layers above.
ARG GIT_SHA
ARG IMAGE_TAG
ENV GIT_SHA=${GIT_SHA} \
    IMAGE_TAG=${IMAGE_TAG}

COPY app ./app

RUN pip install --no-cache-dir .

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
