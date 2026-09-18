"""이 suite 가 어느 배포를 재는지 못박는다.

`Settings` 는 `.env` 를 읽고 `os.environ` 이 그것을 이긴다. 그래서 자기 `.env` 에서
`DEFAULT_MODEL` 을 Bedrock 으로 돌려 둔 개발자 — 바로 이 suite 가 시험하는 설정이다 —
는 `TestClient(app)` 로 session 을 여는 테스트 전부의 답을 바꾸게 된다. 단정 하나도
아니다. `run_config` 는 model 과 거기서 파생된 provider 와 그 model 의 spec 이 들고
있는 reasoning 예산을 함께 보고하고, `tests/test_qa_run_config_contract.py` 는 그
셋을 다 확인한다.

빈 값은 `FALLBACK_MODEL` 을 뜻하므로, suite 는 어느 기계에서 돌든 같은 배포를
설명한다. 다른 기본값을 보고 싶은 테스트는 자기 `Settings` 를 만들어 그것을 읽는
module 을 patch 한다 — `tests/test_llm_default_model.py` 가 그 방법이다.

import 시점에 도는 것이 중요하다. 어떤 테스트 module 이 `app` 을 끌어오기 전이어야
하고, `tests/test_qa_run_config_contract.py:34` 는 module 수준에서 `TestClient(app)`
을 만들기 때문에 fixture 로는 그 시점을 못 잡는다. 지우는 것(`pop`)으로는 안 되는
것도 같은 이유다 — 그러면 `.env` 파일이 다시 이긴다.
"""

import os

os.environ["DEFAULT_MODEL"] = ""
