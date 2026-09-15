# artel-agent-server

- 게임 QA 를 LLM agent 로 돌리는 Python FastAPI 서버
- agent 여섯 — QA 실행, 시나리오 생성, game context 추출, knowledge 검색어 생성,
  screen selector 판정, 스텝 문구 다듬기
- QA 런은 tool 37 개와 middleware 5 개를 두른 tool loop. 게임을 직접 만지지 않고
  artel-orchestration-server 를 거쳐 artel-sdk 가 붙은 Unity 빌드를 조작함
- 사람이 부르는 API 가 아님. 라우트 14 개 중 13 개가 `/internal` 아래 있고 인증이 없음
- 결정론적 명세 발견기 `app/specs_v2` 도 여기 삶. 그쪽은 모델을 한 번도 부르지 않음

| 무엇 | 수 | 어디 |
| --- | --- | --- |
| agent | 6 | `app/agents/` |
| HTTP 라우트 | 12 | `app/api/` |
| WebSocket 라우트 | 2 | `/internal/sessions/{id}`, `/internal/qa-sessions/{id}` |
| QA tool | 37 | `app/agents/qa/tools/` |
| QA middleware | 5 | `app/agents/qa/runner.py` |
| QA `MessageType` | 30 | `app/qa/envelope.py` |
| prompt 계열 | 7 | `app/prompts/` (`qa_run` 은 v16, `scenario` 는 v8 까지) |

## 실행

```bash
cp .env.example .env
python -m pip install -e ".[dev]"
python -m uvicorn app.main:app --reload
python -m pytest
```

- `.env` 는 `app.config.Settings` 가 직접 읽음. 설정 50 개가 전부 `.env.example` 에 있음
- **Redis 가 필요함** — `lifespan` 이 `REDIS_URL` 로 조건 없이 client 를 만들고,
  시나리오 세션 저장소와 QA 세션 저장소가 그 뒤에 삶.
  `REDIS_URL` 은 `.env.example` 에 없었음
- 다만 **Redis 가 죽어도 기동은 성공하고 `/health` 는 200 을 돌려줌** — 연결이 lazy 라
  기동 경로에서 아무도 안 두드림. 실패는 첫 세션 요청에서 나타나므로
  `/health` 를 Redis 감시로 쓰면 안 됨
- 모델 호출은 두 길. `LLM_BASE_URL` 로 가는 OpenAI 호환 경로가 기본이고,
  모델 값이 `bedrock/` 로 시작하면 `ChatBedrockConverse` 로 감
  (`app/llm/chat_model.py:178`). `DEFAULT_MODEL` 은 코드 상수라 `.env` 로 못 바꿈
- LLM 사용량 수집은 `ORCHESTRATION_BASE_URL` 이 비면 통째로 꺼짐. 로컬 실행이 원하는 상태
- 테스트는 트레이싱을 끄고 돌리는 편이 나음 — LangSmith 429 로그가 요약을 덮음

```bash
LANGSMITH_TRACING=false python -m pytest -q
```

## 신뢰 경계

- 공개 표면이 없음. 업무 라우트는 전부 orchestration 만 부르는 서버-투-서버 경로이고
  `/internal` 아래 삶
- **`/internal` 아래 라우트에 인증이 하나도 없음.** `app/api/` 어디에도 `Security`,
  `HTTPBearer`, `Authorization` 를 읽는 코드가 없음
- 노출을 막는 것은 경로가 아니라 배포 구성 — 컨테이너가 `app-net` 에만 붙고
  `docker run` 에 `-p` 가 없으며 리버스 프록시가 이 서비스를 가리키지 않음
- 접두사 밖에 있는 것은 `/health` 와 문서 진입점 `/docs` · `/redoc` · `/openapi.json` 뿐.
  컨테이너 헬스체크와 도구 표면이라 경계와 무관함
- 새 서버-투-서버 라우트는 `/internal` 아래 붙임. 공개 라우트를 처음 붙이는 사람이
  그 시점에 엔드유저 인증을 설계함
- 배포에 `-p` 를 더하거나 공개 호스트를 프록시로 붙이면 그 순간 `/internal` 전체가
  인터넷에 무인증으로 열림
- 자세한 것은 [`.agents/docs/project.md`](.agents/docs/project.md) 의 「API 표면과 신뢰 경계」

## 부팅이 죽는 조건

`create_app` 은 라우터를 붙이기 전에 프롬프트를 전부 읽고 설정을 검사함. 컨테이너가
안 뜨면 거의 이 셋 중 하나임.

| 무엇이 죽나 | 언제 | 무엇을 보나 |
| --- | --- | --- |
| `validate_prompts()` | 프롬프트 frontmatter 의 `placeholders` 가 본문의 `{name}` 과 다를 때 | `PromptError` |
| `validate_prompts()` | `*_PROMPT_VERSION` 이 없는 버전 디렉터리를 가리킬 때 | `PromptError` |
| `Settings.known_model` | `QA_COMPACTION_MODEL` 이 `app/llm/models.py` 카탈로그 밖일 때 | `ValidationError`, 아는 슬러그 목록이 함께 실림 |

- 셋 다 런 중이 아니라 부팅에서 죽는 것이 의도임. 프롬프트가 어긋난 채 뜬 서버는
  한 시간 뒤 깨진 런으로 나타남
- `validate_prompts()` 는 `SETTINGS_VERSION_KEYS` 에 있는 여섯 계열만 버전을 해결함.
  `step_phrasing` 에는 버전 설정이 없음
- 프롬프트 본문은 wheel 에 자동으로 안 실림. `pyproject.toml` 의 `package-data` 가
  `app.prompts` 의 `**/*.md` 를 이름으로 지목하고, 빠지면 이미지가 부팅에서 죽음
- `app/prompts/prompts-lock.json` 은 다른 것을 막음 — 이미 내보낸 버전을 **제자리에서**
  고치는 일. 버전을 더한 뒤에는 `python -m app.prompts.lock --write` 로 다시 만들고,
  merge 충돌은 JSON 을 손으로 고치지 말고 그 명령을 다시 돌려 풀 것

## LangSmith 트레이싱

- 기본으로 꺼져 있음. `.env` 에 둘을 넣으면 켜짐

```dotenv
LANGSMITH_TRACING="true"
LANGSMITH_API_KEY="<key from https://smith.langchain.com/settings>"
```

- `app.observability.configure_langsmith` 가 `create_app` 에서 돌며 이 값들을 프로세스
  환경으로 옮김. LangChain 이 읽는 자리가 거기임
- 프로젝트 이름은 `artel-agent-server-<APP_ENV>`. `LANGSMITH_PROJECT` 가 이김
- EU 리전이나 self-hosted 는 `LANGSMITH_ENDPOINT`
- 플래그만 켜고 키가 없으면 경고를 적고 트레이싱 없이 뜸 — 부팅을 막지 않음

## Container

```bash
docker build --target runtime -t artel-agent-server:local .
docker build --target test -t artel-agent-server:test .
docker run --rm -p 8080:8080 -v ${PWD}/.env:/app/.env:ro artel-agent-server:local
```

- `test` 타깃이 이미지 안에서 `pytest` 를 돌림. git 디렉터리를 안 넣으므로
  prompt lock 검사가 history 가 아니라 `prompts-lock.json` 을 상대로 돎
- `GIT_SHA` 와 `IMAGE_TAG` 는 `runtime` 타깃의 `ARG` 이고 맨 마지막에 선언됨 —
  새 sha 가 위쪽 의존성 레이어를 무효화하지 않게
- QA 런이 그 둘을 기록함. 옛 agent 구조는 소스 트리에 복사본으로 남기지 않고
  그 이미지를 다시 띄워 재현함

## Jenkins

- `Jenkinsfile` 이 branch 이름으로 배포 대상을 정함

| branch | 대상 |
| --- | --- |
| `main`, `operation` | `operation` |
| `develop`, `stage` | `stage` |

- pull request 빌드는 `CHANGE_ID` 같은 multibranch 변수로 알아냄. Dockerfile 의
  `test` 타깃만 돌리고 runtime 컨테이너는 빌드하지도 배포하지도 않음
- Jenkins 작업공간이 `.env.stage` 와 `.env.operation` 을 줘야 함. 고른 쪽이
  컨테이너에 `/app/.env` 로 마운트됨
- Jenkins 호스트에 Docker 와 `app-net` 네트워크가 있어야 함

## 문서

| 문서 | 무엇 |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | agent 여섯이 무엇이고, QA 런이 왜 tool loop 인지 |
| [`docs/qa-protocol.md`](docs/qa-protocol.md) | `MessageType` 30 개, 어느 tool 이 무엇을 기다리는지, 함정 |
| [`docs/adr/`](docs/adr/README.md) | 이 저장소의 모양을 정한 결정 여섯 개 |
| [`docs/api-documentation.md`](docs/api-documentation.md) | OpenAPI 계약을 어디서 보고 무엇을 선언해야 하는지 |
| [`app/specs_v2/README.md`](app/specs_v2/README.md) | 명세 발견기의 evidence graph 와 연결 motif |
| [`.plan/general/`](.plan/general/) | 나머지 결정의 이유 53 건. 주제별 안내는 ADR 색인에 있음 |
| [`AGENTS.md`](AGENTS.md) | `QA_ARCH_LABEL` 을 언제 올리는지, 용어 규칙 |
