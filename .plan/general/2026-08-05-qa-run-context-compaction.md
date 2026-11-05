# 2026-08-05 — QA 런 컨텍스트 압축 (compact the QA run conversation)

- Date: 2026-08-05
- Jira: ARTEL-237
- Status: Implemented

## Goal

QA 런은 `create_agent` 툴 루프로 돌고, 메시지는 런이 끝날 때까지 LangGraph 상태에
계속 쌓인다. `fold_stale_scenes`(ARTEL-180)가 씬 뷰를 접어 가장 큰 증식원을 눌러
줬지만, 접힌 자리표시자·액션 결과 라인·판정 메시지·지식 검색 결과·모델의 추론 텍스트는
그대로 남는다. 스텝이 많은 시나리오는 결국 프로바이더 컨텍스트 한도에 부딪히고,
그 실패는 런 중간에 터져 판정이 절반만 기록된 채 런이 죽는다.

모델 입력 컨텍스트의 90%에 닿으면 오래된 히스토리를 요약으로 바꾸고, 최근 메시지는
원문 그대로 남긴다. 요약 직후에는 우리가 이미 코드로 들고 있는 사실 — 스텝 판정,
미판정 스텝, 다음 스텝, 상시 운영자 지시 — 을 결정론적으로 다시 주입한다. 에이전트가
스스로 부를 수 있는 `compact_context` 툴도 함께 제공한다.

## Non-goals

- 시나리오 대화(`app/sessions`)는 건드리지 않는다. `history_max_turns` 윈도우 유지.
- 씬 뷰 접기를 대체하지 않는다. `fold_stale_scenes`는 그대로 두고 그 위에 얹는다.
- 토큰/비용 수집(`app/llm/usage.py`, `_log_token_usage`)은 이미 있으므로 손대지 않는다.
- 운영자용 WebSocket compact 명령은 만들지 않는다. 트리거는 자동과 에이전트 툴 둘뿐.
- 런 히스토리의 영속화·재개는 범위 밖.
- 기존 프롬프트 버전(v1–v4) 파일은 수정하지 않는다. 버전은 릴리스이므로 v5를 새로 만든다.

## Context / Constraints

### 지금 develop에 있는 것

- `app/agents/qa/runner.py`가 `create_agent(..., middleware=[...])`로 에이전트를 만들고
  `agent.astream(..., stream_mode="updates")`로 돌린다. 미들웨어 네 개가 모두
  `wrap_model_call`이다: `_fold_scene_views`, `QaCaptureVisionMiddleware`,
  `_append_current_scene`, `_log_token_usage`.
- `_append_current_scene`이 매 모델 호출 끝에 `channel.scene.render_now()`를 붙인다.
  **현재 화면은 항상 모델 앞에 있다** — 압축 후 화면을 잃는 문제는 이미 없다.
- `fold_stale_scenes`(`app/agents/qa/context.py`)는 `wrap_model_call`이므로 요청만
  바꾸고 그래프 상태는 그대로다. 즉 **상태에 남은 메시지는 접히지 않은 원본**이다.
- 시스템 프롬프트는 `app/prompts/qa_run/v{1..4}/system.md`. `load_prompt`가 해석하고
  `qa_prompt_version` 미설정 시 최신 버전을 쓴다.
- `build_chat_model(model, reasoning, cache_prompt)`는 `@lru_cache`이고, QA 런은
  `cache_prompt=True`로 Anthropic 프롬프트 캐싱을 켠다.
- `QaCaptureVisionMiddleware`가 `AgentMiddleware` 서브클래스의 사내 선례다.
- 이 브랜치는 develop이 아니라 PR #43(`chore/기본-모델을-GPT-5.6-Luna로-교체-ARTEL-241`) 위에
  쌓는다. 거기서 `gpt_4o_mini`가 `gpt_5_6_luna`로 **교체**되므로 요약 모델 기본값도 루나다.

### langchain 1.3.14 실측

- `langchain.agents.middleware`가 `SummarizationMiddleware`를 내보낸다.
  `before_model`/`abefore_model` 양쪽 구현, 비동기 경로는 `_acreate_summary` →
  `model.ainvoke`.
- 두 훅 모두 `self._should_summarize(messages, total_tokens) -> bool`을 거친다
  (`summarization.py:386`/`:424`). 강제 압축의 유일한 오버라이드 지점.
- `_find_safe_cutoff_point`(`summarization.py:762`)가 `ToolMessage`에서 뒤로 걸어
  대응하는 `tool_call` id를 가진 `AIMessage`를 찾는다. **툴 호출 쌍은 안 쪼개진다.**
- 반환 형태는 `{"messages": [RemoveMessage(REMOVE_ALL_MESSAGES), *summary, *preserved]}`.
- `ChatOpenAI(model="anthropic/claude-sonnet-5", base_url=openrouter).profile`은
  **`None`**. `("fraction", x)` 트리거는 `profile={"max_input_tokens": N}` 없이는
  `__init__`에서 터진다.
- `_acreate_summary`는 모든 예외를 삼키고 `"Error generating summary: ..."` 문자열을
  돌려주는데, `abefore_model`은 그걸 받고도 `RemoveMessage(REMOVE_ALL_MESSAGES)`를
  그대로 반환한다. **요약 LLM 한 번 실패 = 런 히스토리 전멸.** 반드시 막는다.
- `AgentMiddleware`에 `tools` 클래스 기본값이 없다(`hasattr` False). 팩토리가
  `getattr(m, "tools", [])`로 읽으므로 서브클래스가 `__init__`에서 대입해야 한다.
- `before_model`을 가진 미들웨어는 자기 그래프 노드가 된다 — 루프 한 바퀴당 그래프
  스텝이 하나 더 는다.

### 이 설계가 감당해야 하는 제약

1. **상태는 접히지 않았다.** 압축이 `before_model`에서 `state["messages"]`를 세면
   실제 전송량보다 크게 잡혀 필요보다 일찍 터진다. 세기 전에 접어야 한다.
2. **프롬프트 캐싱과 충돌한다.** 압축은 프리픽스를 통째로 바꾸므로 다음 호출은 캐시를
   전혀 읽지 못하고 다시 쓴다. 압축 한 번의 실제 비용에 이게 포함된다 — 스래싱 가드가
   비용 문제인 이유.
3. **fraction은 요약 모델 창을 본다.** `SummarizationMiddleware`가 `("fraction", x)`를
   `self.model.profile`로 푸는데 그 `self.model`은 요약 모델이다. 런 모델과 다른 모델을
   쓰는 것이 이 설계의 전제이므로, fraction을 런 모델의 `max_input_tokens` 기준 절대
   토큰으로 직접 환산해서 넘긴다. 덤으로 `profile` 주입 자체가 불필요해진다.
4. **`app/config.py`는 `app.llm`을 import할 수 없다.** `app.llm.models`를 부르면
   `app/llm/__init__.py`가 돌고 그게 `chat_model`을, `chat_model`이 다시 `app.config`를
   부른다. 요약 모델 설정은 슬러그 문자열 + `field_validator`로 둔다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 완료. 위 Context가 결과다.

- [x] **Step 1-1: `app/llm/models.py`** — `ModelSpec`에 `max_input_tokens: int` 추가,
      7개 항목 전부 채운다. OpenRouter `context_length`에서 `top_provider.max_completion_tokens`를
      뺀 **입력 예산**을 저장한다(이름이 뜻하는 값을 그대로 담기 위해). 실측 카탈로그:
      gpt-4o-mini 128000/16384, gpt-4o 128000/16384, claude-sonnet-5 1000000/128000,
      claude-opus-4.8 1000000/128000, gemini-2.5-flash 1048576/65535,
      gemini-2.5-pro 1048576/65536, gemma-4-free 262144/32768.
      `LLMModel` docstring의 "re-verify against that endpoint"에 이 필드도 포함시킨다.
      `list_models()`에 노출. `int | None`로 두지 않는다 — 값이 없으면 fraction 트리거가
      조용히 무력화되는데, 그게 최악의 실패 모드다.

- [x] ~~**Step 1-2: `app/llm/chat_model.py`**~~ — 불필요해짐. 임계를 런 모델 기준
      절대 토큰으로 환산하므로 `profile` 주입이 필요 없다. `chat_model.py`는 손대지 않는다.

- [x] **Step 1-3: `app/config.py`** — `qa_prompt_version` 근처에 추가:
      `qa_compaction_enabled: bool = True`(킬 스위치),
      `qa_compaction_trigger_fraction: float = 0.9`,
      `qa_compaction_keep_messages: int = 20`,
      `qa_compaction_min_new_messages: int = 4`(스래싱 가드),
      `qa_compaction_trim_tokens: int = 8000`(langchain 기본 4000은 QA 히스토리엔 작다),
      `qa_compaction_model: str = "openai/gpt-5.6-luna"` + 카탈로그 검증 `field_validator`.
      `keep`이 보존 꼬리의 **크기**를 묶지 않는다는 점, 매 턴 압축이 관찰되면
      `keep=("fraction", 0.3)`이 해법이라는 점을 주석으로 남긴다.

- [x] **Step 2-1: `app/qa/channel.py`** — `on_chat`에서 `operator_instructions` 리스트에도
      기록한다. `drain_operator_messages`는 그대로(1회성 배달 큐). 운영자 말이 지금은
      툴 결과 문자열 안에만 있어서 압축이 지우기 때문. `on_chat`이 모든 운영자 메시지가
      지나는 유일한 통로라서 여기 둔다. 상한은 두지 않는다 — 600초 데드라인이 사실상의
      상한이고, 도달하지 않는 상태를 방어하는 코드는 넣지 않는다.

- [x] **Step 2-2: `app/agents/qa/tools.py`** — `QaRunState`에
      `compaction_requested: bool`, `compactions: int` 추가. `finish_run`의 미보고 스텝
      계산을 `QaRunState` 메서드로 올린다(원장이 같은 집합을 쓴다).

- [x] **Step 3: `app/agents/qa/compaction.py` (신규)** — 핵심.

  - 요약 프롬프트: `app/prompts/qa_compaction/v1/summary.md`. 다른 프롬프트와 같은
    버저닝을 쓴다. `qa_run`의 role이 아니라 **독립 agent**인 이유는 롤백 경로다 —
    `QA_PROMPT_VERSION=v4`가 시스템 프롬프트 전용 롤백인데, v4에 이 파일이 없으므로
    role로 넣으면 부팅이 `PromptError`로 죽는다. 요약은 다른 모델에 가는 다른 호출이다.
    `qa_compaction_prompt_version` 설정과 `SETTINGS_VERSION_KEYS` 등록이 따라온다.
    로더가 `{messages}` 자리표시자 선언을 검증해준다는 것이 덤이 아니라 실익이다.
    langchain 기본 프롬프트는 `## ARTIFACTS`(파일/경로)를 요구해
    게임 런에 무의미하다. 섹션을 바꾼다 — `## SCENARIO`(제목·의도·전체 스텝 목록),
    `## WHAT HAS BEEN TRIED`(무엇을 어느 요소에 했고 게임이 어떻게 반응했는지, 실패는 왜),
    `## GAME BEHAVIOUR LEARNED`(화면 이동 경로, 대사 넘기는 키, 기다려야 하는 것 —
    히스토리에만 있는 값비싼 지식), `## OPEN PROBLEMS`, `## NEXT ACTION`.
    "스텝의 pass/fail을 절대 쓰지 말 것 — 판정은 다른 곳에 기록되며 네 판정은 그것과
    충돌한다"를 명시. `<messages>` 마커와 `{messages}` 자리표시자는 유지(상수의 계약).
    요약문은 런 언어와 무관하게 영어로 — 모델용이다.

  - `QaCompactionMiddleware(SummarizationMiddleware)`. `__init__`에서
    `trigger=("fraction", f)`, `keep=("messages", n)`, `summary_prompt=QA_SUMMARY_PROMPT`,
    `trim_tokens_to_summarize=...`, 그리고
    **`token_counter=lambda msgs: count_tokens_approximately(fold_stale_scenes(msgs))`** —
    접힌 상태가 실제 전송량이므로 그걸 센다. `self.tools = [compact_context]` 대입.

  - `_should_summarize` 오버라이드: 스래싱 가드 → 강제 플래그 → `super()`.
    비공개 메서드이므로 함께 넣는다: `pyproject.toml`의 `langchain>=1.0`을
    `langchain>=1.3.14,<2`로 조인다(현재 범위는 이 이음매가 없는 버전을 허용한다),
    그리고 Step 4의 카나리아 테스트. 조용한 실패가 위험한 모드다.

  - `abefore_model` 오버라이드:
    1. `compaction_requested`를 첫 `await` 전에 동기적으로 소비(재진입 이중 발화 방지).
    2. `super().abefore_model({**state, "messages": fold_stale_scenes(state["messages"])}, runtime)`
       — 접힌 목록을 넘기면 세기·자르기·요약·보존이 전부 접힌 것 위에서 일어나고,
       보존된 꼬리도 접힌 사본으로 상태에 되돌아간다. 접기는 멱등이고 타임라인·콘솔은
       채널/로거를 읽으므로 영향 없음. 공개 시그니처만 쓴다.
    3. 요약문이 `"Error generating summary:"` / `"Previous conversation was too long"`으로
       시작하면 **`None`을 반환**하고 `LogCategory.SYSTEM` 노트를 남긴다. 히스토리 전멸 방지.
    4. 요약 뒤에 `render_progress_ledger(...)`를 `HumanMessage`로 끼운다. 경계는
       `additional_kwargs["lc_source"] == "summarization"`으로 찾고, 못 찾으면 인덱스 1로
       폴백(`lc_source`도 준공개).
    5. `compactions += 1`, 콘솔 로그(`_clip` 재사용)와 타임라인 노트.
    동기 `before_model`은 상속된 채로 둔다(팩토리가 async 노드를 등록한다).

  - `render_progress_ledger(run_state, channel) -> str`: 생성하는 것이지 요약하는 게
    아니다. 스텝 판정 전부(`step / PASS|FAIL / message`), 미판정 스텝 목록, 다음 스텝 번호,
    그리고 `channel.operator_instructions`를 툴이 쓰는 문구
    (`with_operator_messages`) 그대로. **현재 화면은 넣지 않는다** —
    `_append_current_scene`이 매 호출에 이미 붙인다.

  - `compact_context(reason)` 툴: `run_state.compaction_requested = True`만 세우고
    "다음 턴 전에 압축된다, 판정·스텝·운영자 지시는 보존되고 바로 다시 알려준다,
    하던 데서 이어가라"를 돌려준다. `build_tools`가 아니라 미들웨어의 `self.tools`로
    등록한다 — 미들웨어를 빼면 툴도 같이 빠져야 한다.
    툴이 `Command(update=...)`를 반환하는 대안을 쓰지 않는 이유: 툴 자신의 `AIMessage`가
    이미 상태에 있어서 `REMOVE_ALL_MESSAGES` 쓸이에 걸리는데 `ToolMessage`는 `Command`가
    새로 넣는 것이라 짝이 깨진다(dangling `tool_call_id` → 프로바이더 400). 게다가 모델
    핸들·트림·프롬프트·컷오프가 두 벌이 된다.

- [x] **Step 4: `app/prompts/qa_run/v5/` + `app/prompts/qa_compaction/v1/`** — v4를 복사하고 `system.md`에 3문장 추가:
      히스토리가 요약 + 재진술 원장으로 바뀔 수 있다, 원장이 권위이며 판정이 적힌 스텝은
      다시 하지 않는다, `compact_context`가 있고 언제 부르는지. `vision_directive.md`는
      그대로 복사.

- [x] **Step 5: `app/agents/qa/runner.py` 배선**
      1. `__init__`에서 `get_settings()`로 압축 설정을 읽는다(테스트 주입용 인자 허용).
         `QaExecutionService`의 `runner_factory`를 안 건드리기 위해.
      2. `run()`에서 `middleware` 리스트 앞에 `QaCompactionMiddleware`를 넣는다.
         요약 모델은 `build_chat_model(settings.qa_compaction_model)` —
         `cache_prompt`는 끈다(일회성 호출이라 캐싱이 손해).
      3. **`recursion_limit`**: 지금 `* 2`(model + tools). 압축 노드가 하나 더 붙으므로
         그대로 두면 툴 예산이 1/3 깎인다. `before_model` 미들웨어 수를 반영한다.
      4. **`_log_reasoning`**: `update.values()`가 노드 이름을 안 본다. 압축 노드가 보존
         꼬리를 통째로 다시 뱉으므로 모든 `AIMessage`가 타임라인에 두 번 올라간다.
         `update.items()`로 바꾸고 `model`/`tools` 외 노드는 건너뛴다. **이유를 주석으로** —
         다음 사람이 되돌리고 싶어진다.

- [x] **Step 6: Tests** — 아래 Validation.

- [x] **Step 7: Rollout** — `qa_compaction_enabled=True`로 나간다. 문제가 생기면
      환경변수로 끈다(코드 배포 불필요). 프롬프트는 `qa_prompt_version=v4`로 되돌릴 수 있다.

## Validation

- **Commands to run:** `python -m pytest`

- **신규 `tests/test_qa_compaction.py`**
  - 툴 호출 쌍 보존: 순진한 `len - keep` 컷오프가 `ToolMessage`에 정확히 떨어지는
    히스토리를 만들고, 살아남은 모든 `ToolMessage`에 대응 `tool_call["id"]`를 가진
    `AIMessage`가 있는지 + 답 없는 `tool_call`이 없는지. 재사용 헬퍼로.
  - **요약 실패가 히스토리를 지우지 않는다**: 페이크의 `ainvoke`가 예외를 던지게 하고
    `abefore_model`이 `None`을 반환하는지, `LogCategory.SYSTEM` 노트가 나갔는지.
    이 파일에서 가장 값진 테스트.
  - 강제 경로가 자동 임계 아래에서도 압축한다 / `compaction_requested`가 정확히 1회 소비.
  - 자동 경로가 `fraction * max_input_tokens` 이상에서 발화, 미만에서 미발화.
  - 스래싱 가드: 압축 직후 `min_new_messages` 미만 추가 시 `None`, 요약 호출 0회.
  - **토큰 카운터가 접힌 크기를 센다**: 접히지 않은 상태로는 임계를 넘지만 접으면
    안 넘는 히스토리에서 압축이 발화하지 않는지.
  - 원장 완전성: pass/fail 섞인 판정 + 운영자 CHAT 2건에서, 결과 메시지가 모든 판정의
    스텝 번호·메시지, 두 운영자 문자열 원문, 다음 스텝 번호를 담는지.
  - `state.step_results`가 압축에 영향받지 않는지.
  - 카나리아: `SummarizationMiddleware._should_summarize`가 `(self, messages,
    total_tokens)` 시그니처로 존재하는지, 기본 클래스 `abefore_model`이 임계 초과
    히스토리에서 `RemoveMessage(REMOVE_ALL_MESSAGES)`로 시작하는 리스트를 반환하는지.
    docstring에 *"우리 코드가 아니라 langchain 업그레이드에서 깨지라고 있는 테스트"*.

- **신규 `tests/test_qa_run_compacted.py`** — 압축이 낀 런이 끝까지 간다.
  실제 `create_agent` 그래프를 스크립트된 페이크 챗 모델로 돌린다:
  `observe_scene` → `click_button` → `report_step` → `compact_context` → `report_step`
  → `finish_run`. `send`가 GAME_STATE/ACTION_RESULT를 자동 응답하는 `QaRunChannel`
  (`tests/test_qa_tools.py`의 헬퍼를 가져온다). `state.finished`, 판정 2건,
  `state.compactions == 1` 확인. 배선을 증명하는 유일한 테스트.

- **확장**: `test_qa_tools.py`(툴이 플래그를 세우는지, 미보고 스텝 헬퍼),
  `test_qa_channel.py`(`on_chat`이 durable하게 기록하면서 `drain`은 여전히 1회),
  `test_qa_reasoning_log.py`(`QaCompactionMiddleware.before_model` 키 업데이트가
  타임라인 로그를 만들지 않는지 — 중복 THOUGHT 회귀 고정),
  `test_config.py`(신규 기본값), `test_prompts.py`(v5 자리표시자 검증),
  `test_llm_models.py`(모든 모델에 양수 `max_input_tokens`,
  `build_chat_model(m).profile["max_input_tokens"]` 일치 — `cache_clear()` 필요).

- **Expected output:** 전체 스위트 green.

- **수동 검증:** `qa_compaction_trigger_fraction`을 0.15로 낮춰 실제 시나리오 1회 실행.
  런이 완주하는지, SYSTEM 노트가 타임라인에 뜨는지, 판정이 중복되지 않는지,
  THOUGHT가 두 번 찍히지 않는지. 같은 런에서 `response_metadata["model_provider"]`가
  실제로 오는지 확인 — `count_tokens_approximately`의 usage 스케일링이 여기 의존한다.

## Risks & Rollback

- **Risks:**
  - `_should_summarize` 비공개 API 의존. 버전 핀 + 카나리아로 막지만, 조용히 압축이
    멈추면 한참 뒤 프로바이더 400으로 나타난다.
  - 요약 실패 시 히스토리 전멸(langchain 기본 동작). Step 3-3 가드가 유일한 방어.
  - 요약 호출이 그래프 안, 600초 데드라인 안에서 블로킹된다. 값싼 요약 모델 고정으로
    묶지만 관찰 필요.
  - 압축이 Anthropic 프롬프트 캐시를 통째로 무효화한다. 압축 1회 비용 = 요약 호출 +
    다음 호출의 캐시 재작성.
  - 토큰 추정이 프록시 위의 근사다 — `state["messages"]`에 시스템 프롬프트와 툴 스키마가
    빠져 있다. usage 스케일링이 보정하지만 `model_provider` 유무에 달렸다. 어긋나면
    `max_input_tokens` 값을 깎는 게 대응(한 곳 수정).
  - 에이전트가 `compact_context`를 남발할 수 있다. 스래싱 가드가 비싸지 않게 만들고,
    툴 결과가 "다시 압축하지 말고 이어가라"고 말한다. 텔레메트리가 보여주면 런당 상한.

- **Rollback steps:** `QA_COMPACTION_ENABLED=false`. 프롬프트만 되돌리려면
  `QA_PROMPT_VERSION=v4`. 코드 전체는 `git revert`.

## Open Questions

- 없음. 남은 것은 수동 검증 하나: 실제 런에서 `response_metadata["model_provider"]`가
  오는지 확인. 안 오면 `count_tokens_approximately`의 usage 스케일링이 꺼진 채로
  근사만 쓰게 되고, 대응은 `ModelSpec`의 `max_input_tokens`를 깎는 것이다.
