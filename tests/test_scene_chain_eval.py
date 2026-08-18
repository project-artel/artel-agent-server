"""ARTEL-457 측정 하네스. 네트워크를 타지 않는다 — 모델 응답은 전부 손으로 쓴 것이다.

제일 중요한 것은 `golden_answer_key_is_internally_consistent` 다. 답안지가 조용히 틀리면
세 arm 의 수치가 전부 뜻을 잃는데, 그것을 사람이 눈으로 지킬 수는 없다.
"""

import json
from pathlib import Path

import pytest

from evals.scene_chain.arms import Arm, build_arm_input
from evals.scene_chain.citations import (
    Citation,
    MalformedOutput,
    Role,
    Verdict,
    check_chain,
    parse_chains,
    resolve,
    verify,
)
from evals.scene_chain.evidence import (
    Capture,
    ContentMap,
    join_links,
    mechanical_join,
    names_a_state,
)
from evals.scene_chain.scoring import (
    emits_supported,
    emits_unsupported,
    join_baseline_checks,
    load_golden_chains,
    score_run,
    summarize,
)

DATA = Path("evals/scene_chain/data")


@pytest.fixture
def content_map() -> ContentMap:
    return ContentMap.load(DATA / "golden-content-map.json")


@pytest.fixture
def golden():
    return load_golden_chains(DATA / "golden-chains.json")


def chain_payload(*citations: dict) -> dict:
    return {"chains": [{"summary": "t", "chain": list(citations)}]}


def cite(**kwargs) -> dict:
    return {"capabilityId": None, "unit": None, **kwargs}


def test_content_map_indexes_every_capability(content_map):
    assert len(content_map) == 18
    assert content_map.by_id[24].writes == frozenset(
        {"MapMove.StagePosition", "GameClearScene"}
    )
    assert "BattleWaveController.wave" in content_map.by_id[24].reads


def test_unit_lookup_collects_every_row_sharing_a_method(content_map):
    assert {row.capability_id for row in content_map.matching_unit("Map.MapMove.CharacterMove")} == {
        10,
        11,
        12,
    }
    # 코루틴 상태 기계 이름은 원래 메서드로 되돌아간다.
    assert {
        row.capability_id
        for row in content_map.matching_unit("Combat.Enemies.BattleWaveController.WaveEndSensor")
    } == {23, 24}


def test_a_state_and_its_member_name_the_same_thing():
    assert names_a_state("CombineButton.combineZone.activeSelf", "CombineButton.combineZone")
    assert names_a_state("CombineButton.combineZone", "CombineButton.combineZone.activeSelf")
    # 이름이 비슷하다고 같은 것이 아니다 — 이것이 갈리지 않으면 실험 전체가 무의미하다.
    assert not names_a_state("MapMove.position", "MapMove.StagePosition")


def test_resolution_reads_an_id_a_unit_and_refuses_an_invented_id(content_map):
    assert resolve(Citation(23, None, Role.writes, "x"), content_map) == frozenset({23})
    assert resolve(Citation(None, "Map.MapMove.CharacterMove", Role.writes, "x"), content_map) == (
        frozenset({10, 11, 12})
    )
    # 없는 id 는 `unit` 이 맞아도 구제하지 않는다.
    assert resolve(
        Citation(771, "Map.MapMove.CharacterMove", Role.writes, "x"), content_map
    ) == frozenset()


def test_verification_separates_map_capture_and_invention(content_map):
    capture = Capture(
        {
            "types": {
                "Scenes.GameClearController": [
                    {
                        "source": "System.Void Scenes.GameClearController::ShowGettedCard()",
                        "effects": [],
                        "condition": {
                            "kind": "test",
                            "left": "StageDataSingleton.stagePosition",
                            "operator": "==",
                            "right": "0",
                        },
                    }
                ]
            }
        }
    )
    in_map = verify(Citation(24, None, Role.writes, "MapMove.StagePosition"), content_map, capture)
    assert in_map.verdict is Verdict.in_map

    out_of_map = verify(
        Citation(None, "Scenes.GameClearController.ShowGettedCard", Role.reads,
                 "StageDataSingleton.stagePosition"),
        content_map,
        capture,
    )
    assert out_of_map.verdict is Verdict.in_capture

    invented = verify(
        Citation(20, None, Role.reads, "StageDataSingleton.stagePosition"), content_map, capture
    )
    assert invented.verdict is Verdict.unverified


def test_a_chain_fails_when_any_one_citation_fails(content_map):
    chain = parse_chains(
        chain_payload(
            cite(capabilityId=13, role="writes", via="StageDataSingleton.stagePosition"),
            cite(capabilityId=20, role="reads", via="StageDataSingleton.stagePosition"),
        )
    )[0]
    check = check_chain(chain, content_map, None)
    assert [item.verdict for item in check.checks] == [Verdict.in_map, Verdict.unverified]
    assert check.fabricated


@pytest.mark.parametrize(
    "payload",
    [
        {"nope": []},
        {"chains": [{"summary": "x"}]},
        {"chains": [{"summary": "x", "chain": []}]},
        {"chains": [{"summary": "x", "chain": [{"role": "wobbles", "via": "a.b"}]}]},
        {"chains": [{"summary": "x", "chain": [{"role": "reads", "via": "  "}]}]},
        {"chains": [{"summary": "x", "chain": [{"role": "reads", "via": "a.b", "capabilityId": "7"}]}]},
    ],
)
def test_output_that_breaks_the_schema_is_refused(payload):
    with pytest.raises(MalformedOutput):
        parse_chains(payload)


def test_mechanical_join_is_fixed_for_this_capture(content_map):
    pairs = mechanical_join(content_map)
    assert len(pairs) == 22
    assert len([pair for pair in pairs if not pair.is_self_pair]) == 14
    # 22쌍은 서로 다른 사실 7개다. 한 메서드가 기능 행 셋으로 갈린 곳이 같은 사실을 여섯 번 센다.
    links = join_links(content_map, pairs)
    assert len(links) == 7
    assert ("Map.MapMove.CharacterMove", "Map.MapMove.CharacterMove", "MapMove.position") in {
        (link.writer_unit, link.reader_unit, link.state) for link in links
    }


def test_a_chain_mixing_two_states_is_refused(content_map):
    """쓰는 쪽에 StagePosition 을, 읽는 쪽에 position 을 댄 것 — 이름 기반 오분류의 모양.

    인용은 각자 통과한다. 체인으로 묶은 것만 근거가 없다.
    """
    chain = parse_chains(
        chain_payload(
            cite(capabilityId=24, role="writes", via="MapMove.StagePosition"),
            cite(capabilityId=12, role="reads", via="MapMove.position"),
        )
    )[0]
    check = check_chain(chain, content_map, None)
    assert all(item.passed for item in check.checks)
    assert not check.names_one_state
    assert not check.passed


def test_golden_answer_key_is_internally_consistent(content_map, golden):
    """답안지가 스스로 맞는지 — 이 테스트가 깨지면 세 arm 의 수치를 믿을 수 없다."""
    assert len(golden) == 10
    supported = [item for item in golden if item.supported]
    unsupported = [item for item in golden if not item.supported]
    assert len(supported) == 7
    assert len(unsupported) == 3
    # 골든이 전부 기계 조인 안에 있으면 grep 이 만점을 받는다. 둘은 밖에 있어야 한다.
    assert len([item for item in supported if not item.reachable_by_join]) == 2

    joined = {(pair.writer, pair.reader, pair.state) for pair in mechanical_join(content_map)}
    for item in supported:
        if not item.reachable_by_join:
            assert item.reader.capability_id is None, f"{item.id} 는 맵 밖이어야 한다"
            continue
        key = (item.writer.capability_id, item.reader.capability_id, item.via)
        assert key in joined, f"{item.id} 는 기계 조인에 없다"
        chain = parse_chains(
            chain_payload(
                cite(capabilityId=item.writer.capability_id, role="writes", via=item.via),
                cite(capabilityId=item.reader.capability_id, role="reads", via=item.via),
            )
        )[0]
        assert check_chain(chain, content_map, None).passed, f"{item.id} 가 인용 대조를 통과 못 한다"

    for item in unsupported:
        key = (item.writer.capability_id, item.reader.capability_id, item.via)
        assert key not in joined, f"{item.id} 가 조인에 있다"
        chain = parse_chains(
            chain_payload(
                cite(capabilityId=item.writer.capability_id, role="writes", via=item.via),
                cite(capabilityId=item.reader.capability_id, role="reads", via=item.via),
            )
        )[0]
        assert not check_chain(chain, content_map, None).passed, f"{item.id} 가 통과해 버린다"


def test_the_out_of_map_goldens_are_grounded_only_in_the_capture(content_map, golden):
    """SC-6·SC-7 의 읽는 쪽은 content_map 에 없고 캡처에만 있다. 그것이 arm 비교의 알맹이다."""
    capture = Capture(
        {
            "types": {
                "Scenes.GameClearController": [
                    {
                        "source": "System.Void Scenes.GameClearController::ShowGettedCard()",
                        "condition": {"kind": "test", "left": "MapMove.StagePosition",
                                      "operator": "==", "right": "1"},
                    },
                    {
                        "source": "System.Void Scenes.GameClearController::Start()",
                        "condition": {"kind": "test", "left": "StageDataSingleton.stagePosition",
                                      "operator": "==", "right": "4"},
                    },
                ]
            }
        }
    )
    for item in (item for item in golden if item.supported and not item.reachable_by_join):
        assert not content_map.matching_unit(item.reader.unit), f"{item.id} 읽는 쪽이 맵에 있다"
        chain = parse_chains(
            chain_payload(
                cite(capabilityId=item.writer.capability_id, role="writes", via=item.via),
                cite(unit=item.reader.unit, role="reads", via=item.via),
            )
        )[0]
        check = check_chain(chain, content_map, capture)
        assert check.passed
        assert [c.verdict for c in check.checks] == [Verdict.in_map, Verdict.in_capture]
        assert emits_supported(item, [check])


def test_the_join_only_baseline_is_reported_as_the_null_hypothesis(content_map, golden):
    """모델 없이 문자열만 맞춰도 8/10 이다. 이 바닥을 못 넘는 arm 은 grep 보다 나을 게 없다."""
    score = score_run("join-baseline", 0, join_baseline_checks(content_map), golden, content_map)
    assert score.golden_correct == 8
    assert score.missed_supported_ids == ["SC-6", "SC-7"]
    assert score.emitted_unsupported_ids == []
    assert score.fabrication_rate == 0.0
    assert score.under_connection == 0


def _answer(content_map, items, use_unit_for_reader=False):
    payload = {"chains": []}
    for item in items:
        reader = (
            cite(unit=item.reader.unit, role="reads", via=item.via)
            if use_unit_for_reader or item.reader.capability_id is None
            else cite(capabilityId=item.reader.capability_id, role="reads", via=item.via)
        )
        payload["chains"].append(
            {
                "summary": item.id,
                "chain": [
                    cite(capabilityId=item.writer.capability_id, role="writes", via=item.via),
                    reader,
                ],
            }
        )
    return [check_chain(chain, content_map, _capture()) for chain in parse_chains(payload)]


def _capture() -> Capture:
    return Capture(
        {
            "types": {
                "Scenes.GameClearController": [
                    {
                        "source": "System.Void Scenes.GameClearController::ShowGettedCard()",
                        "condition": {"kind": "test", "left": "MapMove.StagePosition",
                                      "operator": "==", "right": "1"},
                    },
                    {
                        "source": "System.Void Scenes.GameClearController::Start()",
                        "condition": {"kind": "test", "left": "StageDataSingleton.stagePosition",
                                      "operator": "==", "right": "4"},
                    },
                ]
            }
        }
    )


def test_a_perfect_answer_scores_ten_of_ten(content_map, golden):
    checks = _answer(content_map, [item for item in golden if item.supported])
    score = score_run("a", 1, checks, golden, content_map)
    assert score.golden_correct == 10
    assert score.fabrication_rate == 0.0
    assert score.missed_supported_ids == []
    assert score.out_of_map_correct == 2
    # 골든 supported 7개는 조인 링크 7개 중 5개만 덮는다. 나머지는 과소연결로 남는 것이 맞다.
    assert score.under_connection == 2


def test_emitting_the_traps_costs_both_accuracy_and_fabrication(content_map, golden):
    checks = _answer(content_map, golden)
    score = score_run("a", 1, checks, golden, content_map)
    assert score.golden_correct == 7
    assert score.emitted_unsupported_ids == ["SC-10", "SC-8", "SC-9"]
    assert score.fabrication_rate == pytest.approx(0.3)


def test_a_trap_split_across_two_states_still_counts_as_emitted(content_map, golden):
    """지어냄은 쓰는 쪽과 읽는 쪽에 서로 다른 상태를 대는 모양으로도 온다."""
    sc9 = next(item for item in golden if item.id == "SC-9")
    checks = [
        check_chain(chain, content_map, None)
        for chain in parse_chains(
            chain_payload(
                cite(capabilityId=24, role="writes", via="MapMove.StagePosition"),
                cite(capabilityId=12, role="reads", via="MapMove.position"),
            )
        )
    ]
    assert emits_unsupported(sc9, checks)


def test_a_legitimate_chain_is_not_mistaken_for_the_trap_beside_it(content_map, golden):
    """SC-2(24 -> 10 via StagePosition)와 SC-9(24 -> 12 via position)는 arm (b) 에서
    양 끝이 같은 메서드로 풀린다. `via` 가 둘을 갈라야 한다."""
    sc2 = next(item for item in golden if item.id == "SC-2")
    sc9 = next(item for item in golden if item.id == "SC-9")
    checks = [
        check_chain(chain, content_map, None)
        for chain in parse_chains(
            chain_payload(
                cite(unit="Combat.Enemies.BattleWaveController.WaveEndSensor",
                     role="writes", via="MapMove.StagePosition"),
                cite(unit="Map.MapMove.CharacterMove", role="reads", via="MapMove.StagePosition"),
            )
        )
    ]
    assert emits_supported(sc2, checks)
    assert not emits_unsupported(sc9, checks)


def test_a_supported_golden_cited_by_unit_still_counts(content_map, golden):
    sc3 = next(item for item in golden if item.id == "SC-3")
    checks = [
        check_chain(chain, content_map, None)
        for chain in parse_chains(
            chain_payload(
                cite(unit="Map.MapMove.CharacterMove", role="writes", via="MapMove.position"),
                cite(unit="Map.MapMove.CharacterMove", role="reads", via="MapMove.position"),
            )
        )
    ]
    assert emits_supported(sc3, checks)


def test_summary_pools_fabrication_and_spreads_accuracy(content_map, golden):
    clean = score_run("a", 1, _answer(content_map, [g for g in golden if g.supported]), golden, content_map)
    dirty = score_run("a", 2, _answer(content_map, golden), golden, content_map)
    rolled = summarize([clean, dirty])["a"]
    assert rolled["runs"] == 2
    assert rolled["accuracy"]["min"] == pytest.approx(0.7)
    assert rolled["accuracy"]["max"] == pytest.approx(1.0)
    # 7 + 10 = 17 체인 중 지어냄 3건.
    assert rolled["fabricationRatePooled"] == pytest.approx(3 / 17, abs=1e-4)


def test_the_three_arms_share_one_prompt_and_differ_only_in_evidence():
    content_map_text = (DATA / "golden-content-map.json").read_text(encoding="utf-8")
    pseudo = "// ===== X.cs =====\nclass X { }\n"
    inputs = {arm: build_arm_input(arm, content_map_text, pseudo) for arm in Arm}
    assert len({value.system for value in inputs.values()}) == 1
    assert len({value.evidence_sha256 for value in inputs.values()}) == 3
    assert "capabilityId` 는 null" in inputs[Arm.pseudo_cs].human
    assert content_map_text in inputs[Arm.both].human
    assert pseudo in inputs[Arm.both].human


def test_an_invented_capability_id_is_not_rescued_by_a_real_unit(content_map):
    """arm (c) 는 두 칸을 모두 채우라고 지시받는다. 없는 id 가 `unit` 으로 살아나면
    거기서 지어냄이 통째로 안 세어진다."""
    check = verify(
        Citation(771, "Scenes.GameClearController.ShowGettedCard", Role.reads, "MapMove.StagePosition"),
        content_map,
        _capture(),
    )
    assert check.verdict is Verdict.unverified
    assert check.capability_ids == frozenset()


def test_mixing_two_states_is_counted_apart_from_fabrication(content_map, golden):
    """지어냄 비율은 '근거를 못 댄 것'이다. 상태를 뒤섞은 것은 다른 실패이므로 따로 센다."""
    checks = [
        check_chain(chain, content_map, None)
        for chain in parse_chains(
            chain_payload(
                cite(capabilityId=24, role="writes", via="MapMove.StagePosition"),
                cite(capabilityId=12, role="reads", via="MapMove.position"),
            )
        )
    ]
    score = score_run("a", 1, checks, golden, content_map)
    assert score.chains_mixed_state == 1
    assert score.chains_fabricated == 0
    assert score.fabrication_rate == 0.0


def test_the_runner_puts_the_baseline_next_to_every_arm(tmp_path, content_map, golden):
    """귀무가설이 표에 없으면 아무도 arm 을 그것과 비교하지 않는다."""
    arm_score = score_run("a", 1, _answer(content_map, [g for g in golden if g.supported]), golden, content_map)
    baseline = score_run("join-baseline", 0, join_baseline_checks(content_map), golden, content_map)
    rolled = summarize([arm_score, baseline])
    assert set(rolled) == {"a", "join-baseline"}
    assert rolled["join-baseline"]["accuracy"]["mean"] == pytest.approx(0.8)
    assert rolled["join-baseline"]["outOfMapCorrect"]["mean"] == 0.0
