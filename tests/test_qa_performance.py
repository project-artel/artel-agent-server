"""`get_performance` 와 tool 결과 끝의 hitch 한 줄 (ARTEL-1223).

orchestration 쪽 endpoint 는 따로 들어간다 (track D). 여기 fixture 는 그 계약의 모양이다 —
`GET /internal/qa-tries/{qaTryId}/performance?sinceFrame=<n>&lastSeconds=<s>` 가
`{"samples": [{frame, at, frameTimeMs, fps, hitchCount, cpuPercent, memoryMb}]}` 를 돌려준다.
"""

import asyncio

import httpx

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import ANY_PHASE
from app.qa.channel import QaRunChannel
from app.qa.performance import (
    FAILURE_BACKOFF_SECONDS,
    PerformanceReader,
    PerformanceSample,
    render_summary,
    summarize,
)

SAMPLES = [
    {"frame": 600, "at": "2026-10-23T01:00:00Z", "frameTimeMs": 16.6, "fps": 60.0, "hitchCount": 0, "cpuPercent": 20.0, "memoryMb": 510.0},
    {"frame": 660, "at": "2026-10-23T01:00:01Z", "frameTimeMs": 110.0, "fps": 9.0, "hitchCount": 9, "cpuPercent": 80.0, "memoryMb": 530.0},
    {"frame": 670, "at": "2026-10-23T01:00:02Z", "frameTimeMs": 105.0, "fps": 9.5, "hitchCount": 8, "cpuPercent": None, "memoryMb": 560.0},
]


class FakeOrchestration:
    def __init__(self, samples: list[dict] | None = None, status: int = 200) -> None:
        self.samples = SAMPLES if samples is None else samples
        self.status = status
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, json={"detail": "nope"})
            return httpx.Response(200, json={"samples": self.samples})

        return httpx.MockTransport(handle)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def reader(fake: FakeOrchestration, clock: Clock | None = None) -> PerformanceReader:
    return PerformanceReader(
        77, "http://orchestration:8090/", transport=fake.transport(), clock=clock or Clock()
    )


def test_the_summary_is_average_p95_hitches_and_memory_growth() -> None:
    summary = summarize([PerformanceSample.model_validate(sample) for sample in SAMPLES])
    assert summary.samples == 3
    assert round(summary.average_frame_time_ms, 1) == 77.2
    assert summary.p95_frame_time_ms == 110.0
    assert summary.lowest_fps == 9.0
    assert summary.hitches == 17
    assert (summary.memory_start_mb, summary.memory_end_mb) == (510.0, 560.0)
    assert summary.average_cpu_percent == 50.0
    rendered = render_summary(summary, "the last 10 s")
    assert "frames 600–670" in rendered
    assert "- frame time: average 77.2 ms, p95 110.0 ms" in rendered
    assert "- memory: 510 MB → 560 MB (+50 MB)" in rendered
    assert "`report_issue`" in rendered


def test_a_frame_of_zero_means_no_frame_and_stays_out_of_the_range() -> None:
    """orchestration 은 가까운 frame 이 없으면 `frame: 0` 을 준다."""
    samples = [PerformanceSample.model_validate({**SAMPLES[0], "frame": 0}), PerformanceSample.model_validate(SAMPLES[1])]
    summary = summarize(samples)
    assert (summary.first_frame, summary.last_frame) == (660, 660)
    assert summarize([PerformanceSample.model_validate({**SAMPLES[0], "frame": 0})]).first_frame is None


def test_no_samples_says_so() -> None:
    assert render_summary(summarize([]), "the last 10 s").startswith("No performance samples")


def test_fetch_asks_the_contract_path_with_both_parameters() -> None:
    fake = FakeOrchestration()
    samples = asyncio.run(reader(fake).fetch(since_frame=600, last_seconds=30))
    assert len(samples) == 3
    request = fake.requests[0]
    assert request.url.path == "/internal/qa-tries/77/performance"
    assert dict(request.url.params) == {"sinceFrame": "600", "lastSeconds": "30"}


def test_the_summary_line_counts_recent_hitches_and_is_refreshed_at_most_every_5_seconds() -> None:
    fake = FakeOrchestration()
    clock = Clock()
    probe = reader(fake, clock)
    assert probe.summary_line() is None

    asyncio.run(probe.refresh_summary())
    asyncio.run(probe.refresh_summary())
    assert len(fake.requests) == 1
    assert dict(fake.requests[0].url.params) == {"lastSeconds": "10"}
    assert probe.summary_line().startswith("performance: 17 hitch(es) in the last 10 s")

    clock.now += 5
    fake.samples = [SAMPLES[0]]
    asyncio.run(probe.refresh_summary())
    assert len(fake.requests) == 2
    assert probe.summary_line() is None


def test_a_failing_endpoint_is_left_alone_for_a_minute() -> None:
    fake = FakeOrchestration(status=404)
    clock = Clock()
    probe = reader(fake, clock)
    asyncio.run(probe.refresh_summary())
    clock.now += FAILURE_BACKOFF_SECONDS - 1
    asyncio.run(probe.refresh_summary())
    assert len(fake.requests) == 1
    assert probe.summary_line() is None


def make(fake: FakeOrchestration | None, mode: PhaseCycleMode = PhaseCycleMode.off):
    async def send(frame: dict) -> None:
        return None

    channel = QaRunChannel(qa_try_id=77, send=send, action_timeout=0.05, write_timeout=0.05, reading_wait=0.05)
    state = QaRunState(total_steps=2)
    if fake is not None:
        state.performance = reader(fake)
    arch = resolve_arch(QaArchSpec(vision=VisionMode.off, phase_cycle=mode), "openai/gpt-6-luna")
    tools = {tool.name: tool for tool in build_tools(channel, state, arch)}
    return channel, state, tools


def test_tool_answers_carry_the_hitch_line_once_observe_has_refreshed_it() -> None:
    async def run() -> None:
        channel, _state, tools = make(FakeOrchestration())
        channel.on_pulse({"type": "PULSE", "payload": {"reading": 1, "frame": 10, "scene": "Battle"}})
        looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        assert "performance: 17 hitch(es) in the last 10 s" in looked

    asyncio.run(run())


def test_get_performance_defaults_to_the_last_10_seconds() -> None:
    async def run() -> None:
        fake = FakeOrchestration()
        _channel, _state, tools = make(fake)
        answer = await tools["get_performance"].ainvoke({"step": 1, "thought": "slow?"})
        assert answer.startswith("Performance over the last 10 s (3 one-second sample(s)")
        assert dict(fake.requests[-1].url.params) == {"lastSeconds": "10"}

        since = await tools["get_performance"].ainvoke(
            {"step": 1, "thought": "since the click", "since_frame": 650}
        )
        assert since.startswith("Performance over frames since 650 (")
        assert dict(fake.requests[-1].url.params) == {"sinceFrame": "650"}

    asyncio.run(run())


def test_without_orchestration_the_tool_says_it_cannot_read_and_no_line_appears() -> None:
    async def run() -> None:
        channel, _state, tools = make(None)
        answer = await tools["get_performance"].ainvoke({"step": 1, "thought": "slow?"})
        assert answer.startswith("Performance data could not be read")
        channel.on_pulse({"type": "PULSE", "payload": {"reading": 1, "frame": 10}})
        looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "look"})
        assert "performance:" not in looked

    asyncio.run(run())


def test_get_performance_is_never_refused_by_the_phase_gate() -> None:
    assert "get_performance" in ANY_PHASE
