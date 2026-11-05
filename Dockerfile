FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080

WORKDIR /app

# 의존성은 코드보다 먼저, `pyproject.toml` 만 보고 설치한다. 코드를 복사한 뒤에 `pip install`
# 하면 코드 한 줄이 바뀔 때마다 이 layer 의 캐시가 깨져, 매 빌드가 전체 의존성을 처음부터
# 다시 설치한다. 목록은 `pyproject.toml` 에서 읽으므로 따로 적어 두는 사본은 없다.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

FROM base AS test

RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['dev']))" > /tmp/requirements-dev.txt \
    && pip install --no-cache-dir -r /tmp/requirements-dev.txt

COPY README.md ./
COPY app ./app
COPY tests ./tests

# 의존성은 위에서 끝났다. 여기서는 이 패키지만 설치한다.
RUN pip install --no-cache-dir --no-deps -e .
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

COPY README.md ./
COPY app ./app

RUN pip install --no-cache-dir --no-deps .

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
