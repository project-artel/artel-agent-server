"""실제 TC tool이 사용하는 HTTP 경계만 메모리 응답으로 바꾼다."""

import copy
import httpx


def configure_attempts(channel, state):
    if not state.step_meta:
        state.step_meta = [(index, True) for index in range(1, state.total_steps + 1)]
    requests = []

    async def post(client, path, payload):
        requests.append((path, copy.deepcopy(payload)))
        return httpx.Response(200, json={"id": payload.get("id")}, request=httpx.Request("POST", "http://orch/" + path))

    channel.evaluation.post = post
    channel.evaluation.requests = requests
    return requests


async def start_case(tools, case_id=1):
    return await tools["start_test_case"].ainvoke({"case_id": case_id, "thought": "검증 시작"})


async def report_case(tools, case_id=1, passed=True, **kwargs):
    return await tools["report_test_case"].ainvoke({"case_id": case_id, "verdict": "PASSED" if passed else "FAILED",
        "message": "관측 결과", "thought": "판정", **kwargs})
