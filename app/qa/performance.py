"""게임의 `PERFORMANCE` 지표를 orchestration 에서 읽어 오는 곳 (ARTEL-1223).

SDK 는 1초마다 `PERFORMANCE` (frame time, fps, hitch 수, CPU, 메모리)를 보내고 orchestration 이
저장한다. 이 서버는 그 frame 을 받지 않는다 — 중계되는 것은 `PULSE` 와 `ACTION_RESULT` 뿐이다.
그래서 `GET {orchestration_base_url}/internal/qa-tries/{qaTryId}/performance` 로 묻는다. 응답은
`{"samples": [{frame, at, frameTimeMs, fps, hitchCount, cpuPercent, memoryMb}]}` 이다 (orchestration
#367~#371). 창을 안 주면 orchestration 은 지난 60초를 주고 표본은 600개까지다. 이쪽은 늘 창을 준다.

예외(ARTEL-1219)와 같은 방식이다. tool 결과에는 "지난 10초 hitch N회" 한 줄만 붙고, 자세한 것은
`get_performance` 로 본다. 성능 이상은 step 판정이 아니라 `report_issue` 로 간다.
"""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

PERFORMANCE_PATH = "/internal/qa-tries/{qa_try_id}/performance"
# tool 결과 끝의 한 줄이 세는 창.
SUMMARY_WINDOW_SECONDS = 10.0
# 그 한 줄을 다시 묻는 간격. macro 는 action 마다 `act` 를 지나므로 간격이 없으면 키 한 번에
# HTTP 한 번이다. SDK 가 1초마다 보내므로 5초 안의 값은 거의 같다.
SUMMARY_REFRESH_SECONDS = 5.0
# 묻다가 실패하면 이만큼 쉰다. orchestration 이 아직 이 endpoint 를 모르는 동안 action 마다
# 기다리지 않게 한다.
FAILURE_BACKOFF_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 2.0


class PerformanceSample(BaseModel):
    """1초 묶음 하나. 필드 이름은 wire 의 camelCase 그대로다."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    frame: int | None = None
    at: str | None = None
    frame_time_ms: float | None = Field(default=None, alias="frameTimeMs")
    fps: float | None = None
    hitch_count: int = Field(default=0, alias="hitchCount")
    cpu_percent: float | None = Field(default=None, alias="cpuPercent")
    memory_mb: float | None = Field(default=None, alias="memoryMb")


class _PerformanceBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    samples: list[PerformanceSample] = Field(default_factory=list)


@dataclass(frozen=True)
class PerformanceSummary:
    samples: int
    first_frame: int | None
    last_frame: int | None
    average_frame_time_ms: float | None
    p95_frame_time_ms: float | None
    lowest_fps: float | None
    hitches: int
    memory_start_mb: float | None
    memory_end_mb: float | None
    average_cpu_percent: float | None


def _percentile(values: list[float], fraction: float) -> float:
    """nearest-rank. 표본이 1초 묶음 수십 개라 보간은 의미가 없다."""
    ordered = sorted(values)
    rank = max(math.ceil(fraction * len(ordered)), 1)
    return ordered[rank - 1]


def summarize(samples: list[PerformanceSample]) -> PerformanceSummary:
    frame_times = [sample.frame_time_ms for sample in samples if sample.frame_time_ms is not None]
    fps_values = [sample.fps for sample in samples if sample.fps is not None]
    memory = [sample.memory_mb for sample in samples if sample.memory_mb is not None]
    cpu = [sample.cpu_percent for sample in samples if sample.cpu_percent is not None]
    # orchestration 의 `frame` 은 가장 가까운 `pulse`·action frame 에서 어림한 값이고, 그런 frame 이
    # 없으면 0 이다. 0 은 frame 범위에 넣지 않는다 — 넣으면 "frames 0–670" 처럼 읽힌다.
    frames = [sample.frame for sample in samples if sample.frame]
    return PerformanceSummary(
        samples=len(samples),
        first_frame=min(frames) if frames else None,
        last_frame=max(frames) if frames else None,
        average_frame_time_ms=sum(frame_times) / len(frame_times) if frame_times else None,
        p95_frame_time_ms=_percentile(frame_times, 0.95) if frame_times else None,
        lowest_fps=min(fps_values) if fps_values else None,
        hitches=sum(sample.hitch_count for sample in samples),
        memory_start_mb=memory[0] if memory else None,
        memory_end_mb=memory[-1] if memory else None,
        average_cpu_percent=sum(cpu) / len(cpu) if cpu else None,
    )


def render_summary(summary: PerformanceSummary, window: str) -> str:
    """`get_performance` 의 답."""
    if summary.samples == 0:
        return (
            f"No performance samples for {window}. The game may not send them, or the "
            "window is too short — SDK sends one per second."
        )
    lines = [f"Performance over {window} ({summary.samples} one-second sample(s)"]
    if summary.first_frame is not None:
        lines[0] += f", frames {summary.first_frame}–{summary.last_frame}"
    lines[0] += "):"
    if summary.average_frame_time_ms is not None:
        lines.append(
            f"- frame time: average {summary.average_frame_time_ms:.1f} ms, "
            f"p95 {summary.p95_frame_time_ms:.1f} ms"
        )
    if summary.lowest_fps is not None:
        lines.append(f"- lowest fps: {summary.lowest_fps:.1f}")
    lines.append(f"- hitches: {summary.hitches}")
    if summary.memory_start_mb is not None and summary.memory_end_mb is not None:
        growth = summary.memory_end_mb - summary.memory_start_mb
        lines.append(
            f"- memory: {summary.memory_start_mb:.0f} MB → {summary.memory_end_mb:.0f} MB "
            f"({growth:+.0f} MB)"
        )
    if summary.average_cpu_percent is not None:
        lines.append(f"- CPU: average {summary.average_cpu_percent:.0f}%")
    lines.append(
        "Performance never decides a step's verdict. A hitch, a frame-time spike or memory "
        "that keeps growing is a defect to file with `report_issue`, with the scene and what "
        "you were doing."
    )
    return "\n".join(lines)


class PerformanceUnavailable(RuntimeError):
    """orchestration 이 없거나, 답하지 않거나, 읽을 수 없는 답을 했다."""


class PerformanceReader:
    """런 하나의 `PERFORMANCE` 를 읽는다. tool 결과 끝의 한 줄은 간격을 두고 다시 묻는다."""

    def __init__(
        self,
        qa_try_id: int,
        orchestration_base_url: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._qa_try_id = qa_try_id
        self._base_url = (orchestration_base_url or "").rstrip("/")
        self._transport = transport
        self._clock = clock
        self._next_refresh_at: float | None = None
        # 마지막으로 읽은 "지난 10초 hitch 수". 아직 못 읽었으면 `None` 이라 줄이 없다.
        self._recent_hitches: int | None = None

    @property
    def available(self) -> bool:
        return bool(self._base_url)

    async def fetch(
        self, since_frame: int | None = None, last_seconds: float | None = None
    ) -> list[PerformanceSample]:
        if not self._base_url:
            raise PerformanceUnavailable("ORCHESTRATION_BASE_URL is not set on this deployment")
        params: dict[str, str] = {}
        if since_frame is not None:
            params["sinceFrame"] = str(since_frame)
        if last_seconds is not None:
            params["lastSeconds"] = f"{last_seconds:g}"
        url = f"{self._base_url}{PERFORMANCE_PATH.format(qa_try_id=self._qa_try_id)}"
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as error:
            raise PerformanceUnavailable(f"orchestration unreachable ({type(error).__name__})") from error
        if response.status_code != httpx.codes.OK:
            raise PerformanceUnavailable(f"orchestration answered {response.status_code}")
        try:
            return _PerformanceBody.model_validate_json(response.content).samples
        except ValidationError as error:
            raise PerformanceUnavailable("orchestration answered an unreadable body") from error

    async def refresh_summary(self) -> None:
        """"지난 10초 hitch N회" 를 간격이 지났으면 다시 읽는다. 실패해도 예외를 내지 않는다."""
        if not self._base_url:
            return
        now = self._clock()
        if self._next_refresh_at is not None and now < self._next_refresh_at:
            return
        try:
            samples = await self.fetch(last_seconds=SUMMARY_WINDOW_SECONDS)
        except PerformanceUnavailable as error:
            logger.info("[performance] summary not refreshed: %s", error)
            self._next_refresh_at = now + FAILURE_BACKOFF_SECONDS
            return
        self._recent_hitches = sum(sample.hitch_count for sample in samples)
        self._next_refresh_at = now + SUMMARY_REFRESH_SECONDS

    def summary_line(self) -> str | None:
        """tool 결과 끝에 붙는 한 줄. hitch 가 없거나 아직 못 읽었으면 `None` 이라 줄이 없다.

        읽는 법을 줄 안에 적는다. system prompt 는 8,000자 상한에 76자가 남아 이 문장이 안 들어간다.
        """
        if not self._recent_hitches:
            return None
        return (
            f"performance: {self._recent_hitches} hitch(es) in the last "
            f"{SUMMARY_WINDOW_SECONDS:g} s — `get_performance` has the detail; a real "
            "problem goes to `report_issue`, not into the step verdict"
        )
