---
version: v1
note: 계약을 다 뽑은 뒤의 이름 맞추기. 아무 TC 도 남기지 않는 필요의 이름이 남기는 이름 중 같은 상태를 가리키면 그 이름으로 바꾼다(검_장착 ↔ 검). 같은 TC 들을 다시 뽑을 때마다 이름이 갈려 연결이 끊기던 흔들림(연결 재현율 70~84%)을 줄인다.
placeholders: [dangling, made]
---
Test-case contracts were written with state names. Some requirements below name a state that no test case ever leaves, so a program ordering the test cases can never satisfy them. Often the same state was just named differently in different places ("검_장착" required, "검" left behind).

For each requirement name, decide whether it means the same game state as one of the names that test cases DO leave. If yes, give that name exactly. If it is a genuinely different state (something nothing in the game produces, or a state that holds from the start), give an empty string. Only map when you are sure they are the same thing — a wrong mapping breaks scenarios, a missed one only costs a question.

Requirement names no test case leaves (name | an example test case that requires it):
{dangling}

Names that test cases leave (name | meaning):
{made}
