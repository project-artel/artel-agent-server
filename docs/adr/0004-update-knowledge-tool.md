# ADR 0004 — `update_knowledge` tool 을 되돌린다

- 상태: 뒤집힘
- 근거: `app/qa/envelope.py` 의 `KNOWLEDGE_UPDATE` 주석, `app/qa/run_config.py`,
  `app/agents/qa/tools/state.py`,
  [`.plan/general/2026-07-29-qa-agent-knowledge-write-tools.md`](../../.plan/general/2026-07-29-qa-agent-knowledge-write-tools.md) (ARTEL-189),
  [`.plan/general/2026-08-05-qa-agent-knowledge-update-tool.md`](../../.plan/general/2026-08-05-qa-agent-knowledge-update-tool.md) (ARTEL-257),
  [`.plan/general/2026-08-06-knowledge-graph-tools-and-screen-map-prompt.md`](../../.plan/general/2026-08-06-knowledge-graph-tools-and-screen-map-prompt.md)

## 결정

- `update_knowledge` 가 QA agent 의 tool 목록에 있습니다. knowledge tool 은 지금 일곱입니다 —
  `search_knowledge`, `record_knowledge`, `update_knowledge`, `forget_knowledge`,
  `link_knowledge`, `unlink_knowledge`, `expand_knowledge`.
- ARTEL-189 는 이 tool 을 **범위에서 뺐고**, ARTEL-257 이 되돌렸습니다. 지운 적은
  없습니다 — 애초에 만들지 않은 것입니다.

## 왜

### 1. 뺀 이유는 안전이 아니라 tool 표면이었습니다

- ARTEL-189 의 비목표에 그대로 적혀 있습니다: 고치려면 지우고 다시 기록한다.
  orchestration 의 `KNOWLEDGE_UPDATE` 경로는 그대로 두되 당분간 호출자를 두지 않는다.
- **안전성이나 데이터 무결성 때문이 아닙니다.** 그 이슈 본문이 대가 둘을 스스로 적어
  두었습니다 — 원자성 상실, 계보 단절.
- 즉 대가를 알고 고른 결정이었고, 그 대가가 나중에 값을 청구했습니다.

### 2. 계보 단절이 실제 비용이 된 순간

- ARTEL-239 가 실행 설정 축 비교를 시작하면서, 「이 런이 쓴 knowledge 가 살아남았나」로
  런을 비교하게 됐습니다.
- 그런데 고치기와 버리기가 **같은 사건**이었습니다 — 둘 다 `DELETE` 하나이고 그 이상
  아무것도 아닙니다.
- 그래서 **자기 knowledge 를 가장 성실히 관리한 모델이 가장 나쁘게 채점됐습니다.**
  고칠수록 지운 것이 늘어나기 때문입니다.
- 근거가 뒤집혔으므로 결정을 되돌렸습니다.

### 3. 읽은 것과 훑은 것을 가릅니다

`update_knowledge` 가 돌아오면서 「무엇을 고칠 자격이 있나」를 정해야 했습니다.

| 무엇 | 뜻 | 무엇을 허락하나 |
| --- | --- | --- |
| `knowledge_seen` | 전문을 읽음 | 고치기, 지우기 |
| `knowledge_glimpsed` | 한 줄로 봄 | link, unlink, expand 의 출발점 |

- `search_knowledge` 가 보여 준 이웃 블록은 **120 자로 잘린 한 줄**이라 읽은 것이
  아닙니다. 그래서 이웃은 `knowledge_glimpsed` 에만 들어갑니다.
- `knowledge_seen` 의 취지는 「읽지 않은 것은 고치거나 지울 수 없다」입니다.
  이름이 그 구분을 집니다.
- `knowledge_glimpsed` 는 pop 하지 않습니다 — 한 번 훑은 것은 계속 훑은 것입니다.

### 4. citation 보고는 knob 이 아니라 상수입니다

- `app/qa/run_config.py` 의 `CITATION_REPORTING = True` 는 설정이 아닙니다.
  `RunConfig.citation_reporting` 에 그대로 실립니다.
- orchestration 은 try 가 끝날 때 그 런의 citation 없는 `knowledge_usage` 행을
  `cited=false` 로 확정합니다.
- **citation 을 보고할 방법이 없었던 런에 그렇게 하면 거짓말이 됩니다.**

| 값 | 뜻 |
| --- | --- |
| `null` | 보고할 수 없었음 |
| `false` | 보고할 수 있었는데 안 했음 |

- 이 둘을 나중에 가를 정직한 방법은 **그때 행에 남긴 표시** 하나뿐입니다.
  이 필드가 생기기 전의 런은 `run_config` 에 키 자체가 없어 영원히 `null` 로 남고,
  그것이 맞습니다.
- 상수로 선언한 이유는 언젠가 보고할 수 없는 build — tool 을 깎아 낸 구조,
  `report_step` 이 없는 구조 — 가 말할 자리를 한 군데 두기 위해서입니다.
- 저쪽에서 `prompt_version` 으로 유도하지 않습니다. 데이터 질문을 버전 매기기 방식에
  묶는 일이고, 자기 설정을 보고한 적 없는 런에 대해서는 답할 수조차 없습니다.

## 거절한 대안

| 대안 | 왜 거절했나 |
| --- | --- |
| 지우고 다시 기록하는 것으로 유지 | 고치기와 버리기가 한 `DELETE` 로 합쳐져 성실한 모델이 나쁘게 채점됨 |
| tool 표면을 계속 작게 유지 | 작은 표면의 값이 비교 가능성보다 싸다는 것이 ARTEL-239 에서 드러남 |
| 훑은 항목도 고칠 수 있게 허용 | 120 자로 잘린 한 줄은 읽은 것이 아님 |
| `citation_reporting` 을 설정으로 | 배포마다 다른 값이면 `null` 과 `false` 의 뜻이 배포별로 갈림 |
| orchestration 이 `prompt_version` 으로 유추 | 데이터 질문을 버전 번호 규칙에 묶고, 설정을 보고 안 한 런에는 답이 없음 |

## 대가

| 대가 | 무엇이 일어나나 |
| --- | --- |
| tool 이 하나 늘어남 | 모델이 고를 것이 하나 더 생기고, `arch_fingerprint` 가 옛 구조와 갈림 |
| `knowledge_seen` 을 관리해야 함 | 읽은 적 없는 id 를 고치려 들면 tool 이 거절함. 상태가 런 수명만큼 자람 |
| 두 집합의 구분이 이름에만 있음 | `seen` 과 `glimpsed` 를 헷갈리면 잘못된 tool 에 자격을 줌 |
| `citation_reporting` 이 언제나 참 | 정직한 값이지만, 보고 못 하는 build 가 생기면 그때 손으로 바꿔야 함 |
| knowledge tool 일곱의 순서가 계약 | `app/qa/run_config.py` 가 그 순서를 저장하고 모델도 그대로 받음. 재배열은 구조 변경 |
