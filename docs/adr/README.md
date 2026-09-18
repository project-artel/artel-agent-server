# Architecture Decision Records

- 이 저장소의 모양을 정한 결정 여섯 개입니다.
- 나머지 결정은 아래 「그 밖의 결정」 이 가리키는 `.plan/general/` 문서에 있습니다.

| ADR | 제목 | 상태 |
| --- | --- | --- |
| [0001](0001-qa-run-tool-loop.md) | QA 실행을 structured output 두 번 호출에서 tool loop 로 바꾼다 | 확정 |
| [0002](0002-embedding-model.md) | embedding 을 text-embedding-3-large 1024 차원으로 정한다 | 확정 |
| [0003](0003-knowledge-search-key.md) | 검색 key 로 본문이 아니라 그 본문을 찾을 질문을 embedding 한다 | 확정 |
| [0004](0004-update-knowledge-tool.md) | `update_knowledge` tool 을 되돌린다 | 뒤집힘 |
| [0005](0005-screen-capture-in-request.md) | `every_call` 의 screen capture 를 대화에 저장하지 않고 요청에만 싣는다 | 뒤집힘 |
| [0006](0006-screen-capture-by-role.md) | screen capture 를 역할별로 2~4 장 싣는다 | 확정, 아직 `develop` 에 없음 |

- 0005 와 0006 의 근거는 plan 문서가 아니라 **미병합 branch 의 commit message** 입니다.
- 0002 와 0003 의 측정 기록은 이 저장소에 없고 Notion 페이지에만 있습니다.

## 그 밖의 결정

- [`.plan/general/`](../../.plan/general/) 에 plan 문서 53 개가 있습니다.
- ADR 로 옮기지 않은 결정의 이유는 전부 거기 있습니다. 아래는 그것을 주제별로 묶은 것입니다.

### QA 런의 루프와 예산

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-25-qa-agent-create-agent-tool-loop.md`](../../.plan/general/2026-07-25-qa-agent-create-agent-tool-loop.md) | tool loop 로 바꾼 설계 전체. [ADR 0001](0001-qa-run-tool-loop.md) 의 원문 |
| [`2026-07-26-qa-agent-diagnostics-loop-and-protocol-cleanup.md`](../../.plan/general/2026-07-26-qa-agent-diagnostics-loop-and-protocol-cleanup.md) | 43 회 반복 루프와 프레임 하나가 소켓을 닫던 문제 |
| [`2026-08-05-qa-run-config-and-arch-versioning.md`](../../.plan/general/2026-08-05-qa-run-config-and-arch-versioning.md) | `run_config` 에 무엇을 확정해 적는지, `QA_ARCH_LABEL` 과 fingerprint |
| [`2026-08-05-qa-agent-does-not-stop-on-a-failed-step.md`](../../.plan/general/2026-08-05-qa-agent-does-not-stop-on-a-failed-step.md) | 스텝 하나가 실패해도 런이 계속되는 이유 |
| [`2026-08-05-qa-run-context-compaction.md`](../../.plan/general/2026-08-05-qa-run-context-compaction.md) | 대화가 context window 를 넘기기 전에 compaction 하는 법, 트리거와 thrash 방지 |
| [`2026-08-13-fix-langchain-summary-failure-regression.md`](../../.plan/general/2026-08-13-fix-langchain-summary-failure-regression.md) | langchain 1.3.15 가 요약 실패 처리를 바꾼 회귀 |

### 모델이 매 호출 읽는 것

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-29-fold-stale-scene-views.md`](../../.plan/general/2026-07-29-fold-stale-scene-views.md) | 지난 씬 뷰를 placeholder 로 fold 하는 이유와 몇 장을 남기는지 |
| [`2026-07-28-expose-aim-coordinates-in-scene-view.md`](../../.plan/general/2026-07-28-expose-aim-coordinates-in-scene-view.md) | 씬 뷰에 조준 좌표를 싣는 이유 |
| [`2026-07-28-expose-non-interactable-visuals-in-scene-view.md`](../../.plan/general/2026-07-28-expose-non-interactable-visuals-in-scene-view.md) | 누를 수 없는 시각 요소도 보여 주는 이유 |
| [`2026-07-28-qa-agent-screen-capture-vision.md`](../../.plan/general/2026-07-28-qa-agent-screen-capture-vision.md) | 찍은 화면을 모델이 보고 판정하는 첫 설계 |

### tool 표면

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-29-tool-descriptions-as-single-source.md`](../../.plan/general/2026-07-29-tool-descriptions-as-single-source.md) | tool 정의를 `@tool` 로 옮기고 설명의 출처를 하나로 만든 이유 |
| [`2026-09-01-split-qa-tools-by-subject.md`](../../.plan/general/2026-09-01-split-qa-tools-by-subject.md) | tool 정의를 주제별 모듈로 나누면서 순서를 계약으로 남긴 방법 |
| [`2026-08-13-fix-stale-tool-descriptions.md`](../../.plan/general/2026-08-13-fix-stale-tool-descriptions.md) | 지식 tool 설명에 남아 있던 옛 전제 |
| [`2026-07-28-add-pointer-and-key-hold-tools.md`](../../.plan/general/2026-07-28-add-pointer-and-key-hold-tools.md) | 마우스 이동·드래그·키 홀드가 따로 필요한 이유 |
| [`2026-08-11-add-axis-input-tools-to-qa-agent.md`](../../.plan/general/2026-08-11-add-axis-input-tools-to-qa-agent.md) | 축과 버튼을 이름으로 직접 모는 tool 이 필요한 이유 |
| [`2026-08-26-reset-game-clears-player-prefs.md`](../../.plan/general/2026-08-26-reset-game-clears-player-prefs.md) | `reset_game` 이 저장 데이터를 지울지 고르는 이유 |
| [`2026-08-05-report-issue-tool.md`](../../.plan/general/2026-08-05-report-issue-tool.md) | 결함 보고 tool 의 모양과 severity 를 스스로 검사하는 이유 |
| [`2026-08-27-log-every-tool-call.md`](../../.plan/general/2026-08-27-log-every-tool-call.md) | `TOOL` 과 `TOOL_RESULT` 를 러너 한 곳에서 내는 이유 |

### knowledge 와 citation

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-29-qa-agent-knowledge-search-tool.md`](../../.plan/general/2026-07-29-qa-agent-knowledge-search-tool.md) | 런이 knowledge 를 검색하는 첫 설계 |
| [`2026-07-29-qa-agent-knowledge-write-tools.md`](../../.plan/general/2026-07-29-qa-agent-knowledge-write-tools.md) | 기록·삭제 tool 과 `update_knowledge` 를 범위에서 뺀 결정. [ADR 0004](0004-update-knowledge-tool.md) |
| [`2026-08-05-qa-agent-knowledge-update-tool.md`](../../.plan/general/2026-08-05-qa-agent-knowledge-update-tool.md) | 그것을 되돌린 이유. [ADR 0004](0004-update-knowledge-tool.md) |
| [`2026-08-06-knowledge-graph-tools-and-screen-map-prompt.md`](../../.plan/general/2026-08-06-knowledge-graph-tools-and-screen-map-prompt.md) | link·unlink·expand 와 `knowledge_seen` / `knowledge_glimpsed` 의 구분 |
| [`2026-08-11-report-knowledge-citations.md`](../../.plan/general/2026-08-11-report-knowledge-citations.md) | 스텝 판정이 무엇에 기댔는지 보고하는 이유 |
| [`2026-08-13-receive-knowledge-write-answers.md`](../../.plan/general/2026-08-13-receive-knowledge-write-answers.md) | 쓰기가 거절과 id 를 답으로 받게 된 경위 |
| [`2026-08-13-verify-write-answer-pairing.md`](../../.plan/general/2026-08-13-verify-write-answer-pairing.md) | 답이 제 요청의 답인지 확인하는 이유 |
| [`2026-09-02-add-part-of-reversed-relation.md`](../../.plan/general/2026-09-02-add-part-of-reversed-relation.md) | graph 탐색 표기에 `PART_OF` 를 넣은 이유 |

### game context 를 knowledge 로

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-22-game-context-knowledge-base-design.md`](../../.plan/general/2026-07-22-game-context-knowledge-base-design.md) | 기획 문서에서 게임 문맥을 뽑는 첫 설계 |
| [`2026-07-22-orchestration-game-context-연동-handoff.md`](../../.plan/general/2026-07-22-orchestration-game-context-연동-handoff.md) | orchestration 과 그 경로를 잇는 계약 |
| [`2026-09-02-extract-response-game-context-to-knowledge-items.md`](../../.plan/general/2026-09-02-extract-response-game-context-to-knowledge-items.md) | `extract` 응답을 knowledge 항목 목록으로 바꾼 이유 |

### screen anchor 와 content map

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-08-27-knowledge-screen-anchor.md`](../../.plan/general/2026-08-27-knowledge-screen-anchor.md) | knowledge 에 `anchor` 를 싣고 검색 결과에 보이는 이유 |
| [`2026-08-27-qa-run-v12-drop-screen-map-add-anchor-rule.md`](../../.plan/general/2026-08-27-qa-run-v12-drop-screen-map-add-anchor-rule.md) | 프롬프트에서 화면 지도 절을 빼고 `anchor` 기준을 넣은 이유 |
| [`2026-08-27-scene-context-in-current-scene-block.md`](../../.plan/general/2026-08-27-scene-context-in-current-scene-block.md) | 현재 씬의 capability 와 anchor knowledge 를 씬 뷰에 넣는 이유 |
| [`2026-08-28-screen-selector-verdict-agent.md`](../../.plan/general/2026-08-28-screen-selector-verdict-agent.md) | 화면 제안 판정을 별도 agent 로 띄운 이유 |
| [`2026-08-28-screen-verdict-and-selector-tools.md`](../../.plan/general/2026-08-28-screen-verdict-and-selector-tools.md) | QA agent 가 지금 화면을 보고 판정 목록을 고치는 tool 둘 |
| [`2026-08-30-write-what-a-run-learned-into-the-map.md`](../../.plan/general/2026-08-30-write-what-a-run-learned-into-the-map.md) | 런이 배운 capability 를 지도에 적는 경로 |

### API 경계

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-08-06-move-api-routes-under-internal-prefix.md`](../../.plan/general/2026-08-06-move-api-routes-under-internal-prefix.md) | 모든 업무 라우트를 `/internal` 아래로 모은 이유 |
| [`2026-08-05-llm-usage-path-internal.md`](../../.plan/general/2026-08-05-llm-usage-path-internal.md) | 사용량 전송 경로를 `/internal/llm-usage` 로 옮긴 이유 |
| [`2026-07-21-scenario-agent-session-websocket-design.md`](../../.plan/general/2026-07-21-scenario-agent-session-websocket-design.md) | 시나리오 세션이 REST 와 WebSocket 을 겸하는 설계 |
| [`2026-07-22-ws-client-close-command.md`](../../.plan/general/2026-07-22-ws-client-close-command.md) | 클라이언트가 세션을 끝내는 `close` 프레임 |
| [`2026-08-07-publish-spec-discovery-v2-api.md`](../../.plan/general/2026-08-07-publish-spec-discovery-v2-api.md) | `specs_v2` 를 internal API 로 내보낸 경위 |

### 모델 선택

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-30-select-qa-model-and-reasoning.md`](../../.plan/general/2026-07-30-select-qa-model-and-reasoning.md) | QA 런이 모델과 reasoning 수준을 고르는 방법 |
| [`2026-08-05-replace-default-model-with-gpt-5-6-luna.md`](../../.plan/general/2026-08-05-replace-default-model-with-gpt-5-6-luna.md) | 기본 모델을 바꾼 이유 |

### prompt 버전

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-29-extract-agent-prompts-to-versioned-files.md`](../../.plan/general/2026-07-29-extract-agent-prompts-to-versioned-files.md) | 프롬프트를 상수에서 버전 디렉터리로 옮긴 이유 |
| [`2026-08-06-lock-released-prompt-versions.md`](../../.plan/general/2026-08-06-lock-released-prompt-versions.md) | 내보낸 버전을 제자리에서 고치지 못하게 막는 lock |

### 시나리오 생성

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-24-add-scenario-output-language.md`](../../.plan/general/2026-07-24-add-scenario-output-language.md) | 출력 언어를 고르게 한 이유 |
| [`2026-07-30-run-scoped-multi-scenario-case-authoring.md`](../../.plan/general/2026-07-30-run-scoped-multi-scenario-case-authoring.md) | 생성을 런 스코프와 복수 시나리오로 넓힌 설계 |
| [`2026-08-17-scenario-reviewed-verification-state.md`](../../.plan/general/2026-08-17-scenario-reviewed-verification-state.md) | `reviewed` 판정 상태의 현재 문제 |

### 기반과 배포

| 문서 | 무엇에 답하나 |
| --- | --- |
| [`2026-07-16-fastapi-skeleton.md`](../../.plan/general/2026-07-16-fastapi-skeleton.md) | 이 서버의 첫 뼈대 |
| [`2026-07-19-add-agent-base-interfaces.md`](../../.plan/general/2026-07-19-add-agent-base-interfaces.md) | agent 공통 인터페이스 |
| [`2026-07-16-add-jenkins-docker-deploy.md`](../../.plan/general/2026-07-16-add-jenkins-docker-deploy.md) | Jenkins 와 Docker 배포의 첫 설정 |
| [`2026-07-19-fix-jenkins-pr-docker-build.md`](../../.plan/general/2026-07-19-fix-jenkins-pr-docker-build.md) | pull request 빌드를 가려내는 방법 |
| [`2026-08-11-fix-feature-branch-jenkins-build.md`](../../.plan/general/2026-08-11-fix-feature-branch-jenkins-build.md) | 피처 branch 빌드가 실패하던 원인 |
| [`2026-07-29-enrich-langsmith-traces.md`](../../.plan/general/2026-07-29-enrich-langsmith-traces.md) | trace 에 어느 식별 정보를 싣는지 |
