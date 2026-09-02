# 2026-09-02 — OpenRouter credit 없이 테스트하도록 Claude 구독을 두 번째 LLM backend 로 붙인다

- Date: 2026-09-02
- Jira: ARTEL-757 (스토리 ARTEL-756)
- Status: In progress

## Goal

OpenRouter API key 에 credit 이 없을 때, 로컬에서 이 서버를 그대로 테스트할 수 있게
Claude Agent SDK 를 두 번째 LLM backend 로 붙인다. Agent SDK 는 이 머신의 `claude` CLI
credential 을 그대로 쓰므로 API key 가 필요 없고, 사용량은 구독의 5시간 window 에서 빠진다.

`LLM_BACKEND` 설정 하나로 명시 전환한다. 다섯 개 호출 지점이 전부 돌아가야 한다 —
`create_agent` 를 쓰는 QA agent 와 scenario agent 를 포함한다.

## Non-goals

- OpenRouter 402 를 보고 자동으로 넘어가는 fallback. 사람이 `.env` 에서 켠다.
- 배포 환경에서의 사용. 이것은 로컬 테스트 경로다.
- `usage.py` 가 보내는 cost 회계를 SDK 쪽 숫자로 맞추는 일. SDK 의 `total_cost_usd` 는
  client 쪽 추정치이고, 구독 사용량은 애초에 달러로 청구되지 않는다.

## Context / Constraints

`build_chat_model` (`app/llm/chat_model.py:23`) 하나가 모든 모델 호출의 입구다. 호출 지점은
다섯 곳이고 필요한 기능이 다르다.

| 호출 지점 | 필요한 것 |
|---|---|
| `app/agents/qa/runner.py:400` | `create_agent` + tools + middleware + `astream` + vision + reasoning |
| `app/agents/scenario/agent.py:67` | `create_agent` + tools + `ToolStrategy` structured output |
| `app/agents/screen_verdict/agent.py:59` | `with_structured_output(method="json_schema", strict=True)` + vision |
| `app/agents/knowledge_query/agent.py:30` | `with_structured_output` |
| `app/agents/qa/runner.py:188` | 평범한 단발 호출 |

### 실측으로 확인한 것 (claude-agent-sdk 0.2.151, `claude` CLI 2.1.258)

API key 환경변수를 전부 지우고 확인했다.

1. **구독 인증이 된다.** 응답에 `RateLimitEvent(rate_limit_type='five_hour')` 가 실려 온다.
2. **옵션을 깎지 않으면 harness 가 통째로 딸려 온다.** `17 * 23` 한 번에 13,148 token 이
   붙는다. `tools=[]`, `mcp_servers={}`, `strict_mcp_config=True`, `setting_sources=[]`,
   `skills=[]` 를 주면 290 token 이 된다. 이 다섯 개는 반드시 준다.
3. **structured output 이 된다.** `output_format={"type": "json_schema", "schema": ...}` 를
   주면 `ResultMessage.structured_output` 에 파싱된 dict 가 온다. JSON Schema draft-07 만 받는다.
4. **vision 이 된다.** streaming input 의 base64 `image` block 으로 들어간다.
5. **tool call 을 실행하지 않고 받아올 수 있다.** tool 을 in-process MCP server 로 선언하고
   `can_use_tool` 에서 `PermissionResultDeny(interrupt=True)` 를 돌려주면, 콜백이 tool 이름과
   인자를 그대로 받고 tool 은 실행되지 않는다.
6. **assistant turn 을 되먹일 수 있다.** streaming input 이 `{"type": "assistant", ...}` 를
   받는다.

### 계약의 구멍 (그대로 구현하고 보고한다)

**구조화된 `tool_use` block 과 tool 선언은 같은 요청에 함께 들어갈 수 없다.** 과거 turn 의
`tool_use` block 을 되먹이면서 `tools` 를 선언하면 CLI 가
`API Error: 400 due to tool use concurrency issues` 로 죽는다. tool 이름을 선언 목록 밖의
것으로 바꿔도 똑같이 죽는다 — 이름이 아니라 block 종류가 문제다.

tool 을 선언하지 않으면 같은 history 가 그대로 통과한다. 그래서 backend 를 두 갈래로 나눈다.

- **tool 이 bind 되지 않은 호출** — history 를 구조화된 block 으로 그대로 싣는다. 온전한
  fidelity. `output_format` 으로 structured output 을 받는다.
- **tool 이 bind 된 호출** — 과거의 tool turn 을 텍스트로 풀어 `<prior_turns>` 안에 싣고,
  tool 은 MCP server 로 선언해 다음 결정만 받는다. 실측에서 모델은 이미 부른 tool 을 다시
  부르지 않고 다음 tool 로 넘어갔다.

두 번째 갈래는 fidelity 를 잃는다. 과거 tool 호출이 진짜 `tool_use` block 이 아니라 텍스트로
보이므로, prompt caching 이 걸리지 않고 모델이 history 를 읽는 정확도가 떨어진다. 로컬
테스트용 backend 라서 받아들인다.

### 모델 이름

`LLMModel` 값은 OpenRouter slug 다. Agent SDK 는 `claude-sonnet-5` 같은 맨 이름을 받는다.
Anthropic 이 아닌 slug (`openai/gpt-5.6-luna` 가 `DEFAULT_MODEL` 이다) 는 구독으로 돌릴 수
없으므로 Claude 모델로 바꿔 싣고, 바꿨다는 사실을 로그에 남긴다. 조용히 바꾸면 어떤 모델이
답했는지 아무도 모른다.

## Approach (Checklist)

- [ ] `app/llm/claude_subscription.py` — `ChatClaudeSubscription(BaseChatModel)` 과
      `build_claude_subscription_chat_model(model, reasoning, cache_prompt)`
- [ ] `app/config.py` — `llm_backend`, 그리고 Anthropic 이 아닌 slug 를 받을 대체 모델
- [ ] `app/llm/chat_model.py` — `build_chat_model` 이 설정을 보고 갈라진다
- [ ] `pyproject.toml` — `claude-agent-sdk` 를 dev extra 에 넣는다 (로컬 테스트 경로다)
- [ ] `.env.example`, `README.md`
- [ ] 테스트 — 모델 호출 없이 SDK 경계를 stub 으로 막고 검증한다

## Validation

- `python -m pytest`
- 다섯 호출 지점을 `LLM_BACKEND=claude_subscription` 으로 실제 한 번씩 돌린다
