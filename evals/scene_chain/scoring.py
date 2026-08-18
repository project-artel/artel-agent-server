"""골든 대비 채점 — 정답률 · 지어냄 비율 · 과소연결.

세 수치가 각각 다른 실패를 잡는다. 정답률은 옳게 이었는가, 지어냄 비율은 대지 못할 근거를
댔는가, 과소연결은 문자열만 맞춰도 닿는 곳을 놓쳤는가. 셋을 하나로 합치지 않는다 —
합친 점수는 어느 실패가 일어났는지 말해 주지 않는다.

`join_baseline_checks` 는 모델을 부르지 않는 귀무가설이다. 골든 절반이 기계 조인 안에
있으므로, 그 점수를 넘지 못한 arm 은 grep 보다 나은 것이 없다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evals.scene_chain.citations import (
    AgentChain,
    ChainCheck,
    CitationCheck,
    Role,
    check_chain,
    parse_chains,
)
from evals.scene_chain.evidence import (
    ContentMap,
    JoinedLink,
    join_links,
    mechanical_join,
    names_a_state,
    normalize_unit,
)


@dataclass(frozen=True)
class Endpoint:
    """골든이 지목한 한쪽 끝. 기능 행이 있으면 id 로, 없으면 캡처의 `Owner.Method` 로."""

    capability_id: int | None
    unit: str | None


@dataclass(frozen=True)
class GoldenChain:
    """손으로 쓴 정답 하나. 읽어서 담기만 한다 — 채점은 아래 함수들이 한다."""

    id: str
    supported: bool
    reachable_by_join: bool
    writer: Endpoint
    reader: Endpoint
    via: str
    why: str = ""
    trap: str | None = None


def load_golden_chains(path: Path) -> list[GoldenChain]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return [
        GoldenChain(
            id=row["id"],
            supported=bool(row["supported"]),
            reachable_by_join=bool(row.get("reachableByJoin", False)),
            writer=Endpoint(row["writer"].get("capabilityId"), row["writer"].get("unit")),
            reader=Endpoint(row["reader"].get("capabilityId"), row["reader"].get("unit")),
            via=row["via"],
            why=row.get("why") or row.get("why_unsupported") or "",
            trap=row.get("trap"),
        )
        for row in document["chains"]
    ]


def endpoint_named_by(endpoint: Endpoint, item: CitationCheck) -> bool:
    """인용 하나가 골든의 그 끝을 가리키는가.

    기능 행 id 로도, 캡처의 `Owner.Method` 로도 가리킬 수 있다. content_map 에 행이 없는 끝
    (`SC-6`·`SC-7` 의 읽는 쪽)은 `unit` 으로만 가리켜지므로, 그것을 못 보는 arm 은 원리적으로
    이 골든을 낼 수 없다 — 그 사실 자체가 입력 형식 비교의 알맹이다.
    """
    if endpoint.capability_id is not None and endpoint.capability_id in item.capability_ids:
        return True
    if endpoint.unit and item.citation.unit:
        return normalize_unit(item.citation.unit) == normalize_unit(endpoint.unit)
    return False


def _names(check: ChainCheck, role: Role, endpoint: Endpoint, state: str) -> bool:
    return any(
        item.citation.role is role
        and names_a_state(item.citation.via, state)
        and endpoint_named_by(endpoint, item)
        for item in check.checks
    )


def emits_supported(golden: GoldenChain, checks: list[ChainCheck]) -> bool:
    """근거가 받치는 골든을 **인용까지 통과한 채로** 냈는가."""
    return any(
        _names(check, Role.writes, golden.writer, golden.via)
        and _names(check, Role.reads, golden.reader, golden.via)
        for check in checks
        if check.passed
    )


def emits_unsupported(golden: GoldenChain, checks: list[ChainCheck]) -> bool:
    """근거 없는 골든을 냈는가. 인용이 떨어졌든 말든 **낸 것 자체가 틀린 것**이다."""
    return any(
        _names_loosely(check, Role.writes, golden.writer, golden.via)
        and _names_loosely(check, Role.reads, golden.reader, golden.via)
        for check in checks
    )


def _names_loosely(check: ChainCheck, role: Role, endpoint: Endpoint, state: str) -> bool:
    """지어냄 판정에는 `via` 를 그 인용에만 걸지 않는다.

    쓰는 쪽에 한 상태를, 읽는 쪽에 다른 상태를 댄 것이 바로 이름 기반 오분류의 모양이다.
    체인 안 어느 인용이라도 골든이 지목한 상태를 가리키면 같은 주장으로 본다.
    """
    mentions_state = any(names_a_state(item.citation.via, state) for item in check.checks)
    return mentions_state and any(
        item.citation.role is role and endpoint_named_by(endpoint, item) for item in check.checks
    )


def reached_links(links: list[JoinedLink], checks: list[ChainCheck], content_map: ContentMap) -> list[JoinedLink]:
    pairs = mechanical_join(content_map)
    reached = []
    for link in links:
        writers = {
            pair.writer
            for pair in pairs
            if _pair_in_link(pair, link, content_map)
        }
        readers = {
            pair.reader
            for pair in pairs
            if _pair_in_link(pair, link, content_map)
        }
        writer_end = [Endpoint(key, link.writer_unit) for key in writers] or [
            Endpoint(None, link.writer_unit)
        ]
        reader_end = [Endpoint(key, link.reader_unit) for key in readers] or [
            Endpoint(None, link.reader_unit)
        ]
        if any(
            any(_names(check, Role.writes, end, link.state) for end in writer_end)
            and any(_names(check, Role.reads, end, link.state) for end in reader_end)
            for check in checks
            if check.passed
        ):
            reached.append(link)
    return reached


def _pair_in_link(pair, link: JoinedLink, content_map: ContentMap) -> bool:
    return (
        content_map.by_id[pair.writer].unit == link.writer_unit
        and content_map.by_id[pair.reader].unit == link.reader_unit
        and pair.state == link.state
    )


def join_baseline_checks(content_map: ContentMap) -> list[ChainCheck]:
    """모델 없이 기계 조인만으로 낼 수 있는 답. 세 arm 이 넘어야 할 바닥이다."""
    pairs = mechanical_join(content_map)
    seen: set[tuple[int, int, str]] = set()
    chains: list[AgentChain] = []
    payload = {"chains": []}
    for pair in pairs:
        key = (pair.writer, pair.reader, pair.state)
        if key in seen:
            continue
        seen.add(key)
        payload["chains"].append(
            {
                "summary": f"{pair.writer} -> {pair.reader} via {pair.state}",
                "chain": [
                    {"capabilityId": pair.writer, "unit": None, "role": "writes", "via": pair.state},
                    {"capabilityId": pair.reader, "unit": None, "role": "reads", "via": pair.state},
                ],
            }
        )
    chains = parse_chains(payload)
    return [check_chain(chain, content_map, None) for chain in chains]


@dataclass
class RunScore:
    """한 번의 실행(arm 하나 × 반복 하나)에 대한 수치 전부."""

    arm: str
    repeat: int
    golden_total: int
    golden_correct: int
    correct_ids: list[str] = field(default_factory=list)
    missed_supported_ids: list[str] = field(default_factory=list)
    emitted_unsupported_ids: list[str] = field(default_factory=list)
    #: content_map 밖에서만 근거가 나오는 골든(SC-6·SC-7) 중 맞은 수.
    out_of_map_correct: int = 0
    out_of_map_total: int = 0
    chains_emitted: int = 0
    chains_fabricated: int = 0
    #: 인용은 다 통과했는데 한 체인이 두 상태를 뒤섞은 것. 지어냄과 다른 실패다.
    chains_mixed_state: int = 0
    citations_in_map: int = 0
    citations_in_capture: int = 0
    citations_unverified: int = 0
    join_links: int = 0
    join_links_reached: int = 0
    duration_seconds: float = 0.0
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    malformed_output: str | None = None

    @property
    def accuracy(self) -> float:
        return self.golden_correct / self.golden_total if self.golden_total else 0.0

    @property
    def fabrication_rate(self) -> float:
        return self.chains_fabricated / self.chains_emitted if self.chains_emitted else 0.0

    @property
    def under_connection(self) -> int:
        return self.join_links - self.join_links_reached

    def as_dict(self) -> dict:
        data = asdict(self)
        data["accuracy"] = round(self.accuracy, 4)
        data["fabricationRate"] = round(self.fabrication_rate, 4)
        data["underConnection"] = self.under_connection
        return data


def score_run(
    arm: str,
    repeat: int,
    checks: list[ChainCheck],
    golden: list[GoldenChain],
    content_map: ContentMap,
) -> RunScore:
    links = join_links(content_map)

    correct, missed, wrongly_emitted = [], [], []
    for item in golden:
        if item.supported:
            (correct if emits_supported(item, checks) else missed).append(item.id)
        elif emits_unsupported(item, checks):
            wrongly_emitted.append(item.id)
        else:
            correct.append(item.id)

    out_of_map = [item for item in golden if item.supported and not item.reachable_by_join]
    verdicts = [item.verdict for chain in checks for item in chain.checks]
    return RunScore(
        arm=arm,
        repeat=repeat,
        golden_total=len(golden),
        golden_correct=len(correct),
        correct_ids=sorted(correct),
        missed_supported_ids=sorted(missed),
        emitted_unsupported_ids=sorted(wrongly_emitted),
        out_of_map_correct=sum(1 for item in out_of_map if item.id in correct),
        out_of_map_total=len(out_of_map),
        chains_emitted=len(checks),
        chains_fabricated=sum(1 for check in checks if check.fabricated),
        chains_mixed_state=sum(1 for check in checks if check.mixed_state),
        citations_in_map=sum(1 for verdict in verdicts if verdict == "in-map"),
        citations_in_capture=sum(1 for verdict in verdicts if verdict == "in-capture"),
        citations_unverified=sum(1 for verdict in verdicts if verdict == "unverified"),
        join_links=len(links),
        join_links_reached=len(reached_links(links, checks, content_map)),
    )


def summarize(scores: list[RunScore]) -> dict:
    """arm 별 평균과 최소–최대. 반복은 분산을 보이려는 것이지 평균을 다듬으려는 것이 아니다."""
    by_arm: dict[str, list[RunScore]] = {}
    for score in scores:
        by_arm.setdefault(score.arm, []).append(score)

    summary = {}
    for arm, runs in sorted(by_arm.items()):
        emitted = sum(run.chains_emitted for run in runs)
        fabricated = sum(run.chains_fabricated for run in runs)
        costs = [run.cost_usd for run in runs if run.cost_usd is not None]
        summary[arm] = {
            "runs": len(runs),
            "accuracy": _spread([run.accuracy for run in runs]),
            "outOfMapCorrect": _spread([float(run.out_of_map_correct) for run in runs]),
            # 반복분을 합쳐서 낸다. 체인 두 개를 낸 run 과 스무 개를 낸 run 의 비율을
            # 단순 평균하면 적게 낸 쪽이 과대 대표된다.
            "fabricationRatePooled": round(fabricated / emitted, 4) if emitted else 0.0,
            "chainsMixedState": sum(run.chains_mixed_state for run in runs),
            "underConnection": _spread([float(run.under_connection) for run in runs]),
            "joinLinks": runs[0].join_links,
            "citationsInCapture": sum(run.citations_in_capture for run in runs),
            "citationsUnverified": sum(run.citations_unverified for run in runs),
            "durationSeconds": _spread([run.duration_seconds for run in runs]),
            "costUsd": round(sum(costs), 6) if costs else None,
            "inputTokens": sum(run.input_tokens for run in runs),
            "outputTokens": sum(run.output_tokens for run in runs),
        }
    return summary


def _spread(values: list[float]) -> dict:
    return {
        "mean": round(sum(values) / len(values), 4) if values else 0.0,
        "min": round(min(values), 4) if values else 0.0,
        "max": round(max(values), 4) if values else 0.0,
    }
