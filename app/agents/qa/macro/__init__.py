"""macro — agent 가 성공한 action 순서를 적어 두고 다시 부른다.

조각이 다섯이고 의존이 한 방향이다.

    grammar  ← 데이터만. 허용 목록과 `(값의 모양, 연산자)` 표.
    model    ← 통과한 tree 의 모양. `frozen` pydantic.
    parser   ← `ast.parse` 로 읽고 허용 node 만 통과시키며 model 을 만든다.
    binding  ← 도착한 값을 `PulseMemory` 에서 읽고 비교를 평가한다.
    runner   ← statement 를 batch 로 쪼개 호출자가 준 `run` 을 반복 호출한다.
    book     ← 한 런의 초안과 등록된 정의.

`eval` 도 `exec` 도 쓰지 않는다. macro 텍스트는 `ast.parse` 로 구조만 읽고, 허용 node
목록에 없는 것을 만나면 거절한다. 통과한 tree 도 실행하지 않는다 — `runner` 가 모델을
읽어 `JsonRpcAction` 을 조립할 뿐이다.

**이 package 는 `app.agents.qa.tools` 를 import 하지 않는다.** 저쪽의 `__init__` 이
`macro_tools` 를 import 하므로 반대 방향을 열면 순환이 된다. `runner` 가 호출자에게
받는 것은 `MacroHost` protocol 두 메서드뿐이다.
"""
