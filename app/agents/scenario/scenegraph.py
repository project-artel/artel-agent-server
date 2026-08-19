"""씬 명세를 읽어 "여기서 저기로 갈 수 있나"에 답한다(프로토타입).

**경로 탐색을 모델에게 시키지 않으려고 있는 모듈이다.** 그래프를 프롬프트에 실어 주면 모델이
읽고 추론해야 하는데, 빠짐없이 훑는 일과 경로를 찾는 일은 모델이 못하는 종류다 —
`list_uncovered_cases`를 툴로 만든 것과 같은 판단이다. 여기서 계산해서 답만 준다.

**모른다고 답하는 것이 이 모듈의 절반이다.** 씬 명세는 명세와 관측에서 나오고 둘 다 전량이
아니므로, 간선이 없다는 것은 그런 길이 없다는 뜻이 되지 못한다. 그래서 답은 셋이다.

    KNOWN    이렇게 가면 된다
    UNKNOWN  가는 길을 모른다 — 지어내지 말고 사용자에게 묻거나 플레이로 알아내야 한다
    SAME     이미 그 상태다, 할 것이 없다

`UNKNOWN`일 때 **무엇을 모르는지**를 같이 낸다. 막는 변수 이름이 곧 콜드플레이가 채워야 할
자리이고, 사용자에게 물어야 할 질문이다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

MAX_DEPTH = 8


def _norm(name: str) -> str:
    """`MapMove.StagePosition` → `StagePosition`.

    같은 변수를 명세는 정규화된 경로로, 사전조건은 짧게 적어서 그대로는 안 맞는다. 마지막
    마디로 맞춘다 — 서로 다른 클래스의 `i`가 겹칠 수 있다는 것은 알려진 한계다.
    """
    return name.strip().strip("`").split(".")[-1]


def _as_pairs(guard):
    """가드를 `(변수, (연산자, 값))`으로 편다. `{v: "3"}` 은 `==` 로 읽는다."""
    for var, val in (guard or {}).items():
        if isinstance(val, (list, tuple)) and len(val) == 2:
            yield var, (str(val[0]), str(val[1]))
        else:
            yield var, ("==", str(val))


def _holds(have: str, op: str, want: str) -> bool:
    """`have op want` 가 참인가. 숫자로 못 읽으면 문자열 같음만 본다."""
    if op == "==":
        return have == want
    if op == "!=":
        return have != want
    try:
        a, b = float(have), float(want)
    except (TypeError, ValueError):
        return True          # 비교할 수 없으면 위반이라고 말하지 않는다
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}.get(op, True)


@dataclass
class Answer:
    kind: str                       # KNOWN | UNKNOWN | SAME
    steps: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)   # 스텝의 `basis`에 그대로 들어갈 간선 id
    blocked_by: str | None = None
    note: str = ""


class SceneGraph:
    """씬 명세 한 벌. 없는 명세로 만들면 모든 질문에 UNKNOWN으로 답한다."""

    def __init__(self, spec: dict | None):
        spec = spec or {}
        self.ok = bool(spec)
        self.scene_functions: dict[str, list[str]] = spec.get("scene_functions", {})
        self.scene_edges: list[dict] = spec.get("scene_edges", [])
        self.repeat_edges: list[dict] = spec.get("repeat_edges", [])
        self.black_boxes: list[dict] = spec.get("black_boxes", [])
        # 변수별 상태 간선: var → [(from, to, action)]
        self.var_edges: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for e in spec.get("function_edges", []):
            var, _, val = (e.get("via") or "").partition("=")
            if var and val:
                self.var_edges[_norm(var)].append(("*", val, e.get("action", "")))
        # `+1`/`-1`로 쓰인다고만 적힌 변수. 목표값을 콕 집는 간선은 없지만 눌러서 올릴 수 있다.
        self.incrementable: set[str] = {_norm(v) for v in spec.get("incrementable_variables", [])}
        # 반복 간선이 올리는 변수
        self.repeat_for: dict[str, dict] = {}
        for r in self.repeat_edges:
            m = re.match(r"\s*([A-Za-z_][\w.]*)", r.get("effect", ""))
            if m:
                self.repeat_for[_norm(m.group(1))] = r

    # ---- 조회 -------------------------------------------------------------------

    def functions(self, scene: str) -> list[str]:
        return self.scene_functions.get(scene, [])

    def scene_hop(self, frm: str, to: str) -> dict | None:
        return next((e for e in self.scene_edges if e["from"] == frm and e["to"] == to), None)

    def path(self, frm_scene: str, frm_state: dict, to_scene: str, to_guard: dict) -> Answer:
        """(씬, 상태)에서 (씬, 가드)로 가는 길."""
        if not self.ok:
            return Answer("UNKNOWN", note="씬 명세가 없다.")

        steps: list[str] = []
        edges: list[str] = []
        if frm_scene != to_scene:
            hop = self.scene_hop(frm_scene, to_scene)
            if hop is None:
                return Answer(
                    "UNKNOWN",
                    blocked_by=f"{frm_scene}→{to_scene}",
                    note=f"{frm_scene}에서 {to_scene}으로 가는 조작이 명세에 없다. "
                         f"진입 경로를 모르는 씬: {', '.join(self.scene_functions and [] or [])}",
                )
            steps.append(f"{hop['action']} ({frm_scene} → {to_scene})")
            edges.append(hop.get("id", f"scene:{frm_scene}->{to_scene}"))
            frm_state = {}          # 씬을 넘으면 아는 상태가 없다

        unmet = [(v, frm_state.get(v), want)
                 for v, want in _as_pairs(to_guard)
                 if frm_state.get(v) is not None and not _holds(frm_state[v], *want)]
        if not unmet:
            return Answer("SAME" if not steps else "KNOWN", steps=steps, edges=edges,
                          note="요구 상태가 이미 만족되었거나 확인할 조건이 없다.")

        for var, have, (op, want) in unmet:
            if self._var_reachable(var, want):
                steps.append(f"{var}가 {op} {want}가 되게 만든다")
                edges.append(f"state:{var}={want}")
                continue
            repeat = self.repeat_for.get(var)
            if repeat is not None:
                steps.append(f"{repeat['action']} — {repeat['repeat_until']}까지 반복 ({repeat['effect']})")
                edges.append(repeat.get("id", f"repeat:{var}"))
                continue
            return Answer(
                "UNKNOWN", steps=steps, edges=edges, blocked_by=var,
                note=f"{var}를 {have}에서 {op} {want}로 바꾸는 방법이 명세에 없다. "
                     f"지어내지 말고, 사용자에게 묻거나 플레이로 알아내야 한다.",
            )
        return Answer("KNOWN", steps=steps, edges=edges)

    def _var_reachable(self, var: str, want: str) -> bool:
        if any(to == want for _, to, _ in self.var_edges.get(var, [])):
            return True
        # 증감으로만 알려진 변수는 조작을 되풀이해 값을 옮길 수 있다. 명세가 그 이상은
        # 말해 주지 않으므로 "몇 번"은 실행하는 쪽에 남긴다.
        return var in self.incrementable and want.lstrip("-").isdigit()

    # ---- 검사 -------------------------------------------------------------------

    def edge_ids(self) -> set[str]:
        ids = {e.get("id") for e in self.scene_edges} | {r.get("id") for r in self.repeat_edges}
        return {i for i in ids if i} | {f"state:{v}={t}" for v, es in self.var_edges.items()
                                        for _, t, _ in es}

    def violations(self, steps: list, cases: dict) -> list[str]:
        """스텝들이 계약을 지켰는지 센다. **판단이 아니라 세기다** — 모델에게 자기 채점을
        시키는 것이 아니라, 나온 결과를 결정적 코드로 대조한다. 그래서 Agent 안에서 돌려도 된다.

        `unknown`을 검사하는 것이 특히 중요하다. 통과 사유로만 두면 전부 `unknown`으로 적는
        것이 제일 싼 통과 방법이 되어 검사가 무의미해진다. 명세가 실제로 아는 길에 `unknown`을
        적었으면 거짓말이므로 되돌린다.
        """
        known = self.edge_ids()
        out: list[str] = []
        prev = None          # 직전 검증 스텝의 (씬, 상태)
        bridged: list[str] = []   # 그 뒤로 지나온 브리지들의 basis

        for i, st in enumerate(steps, 1):
            basis = (getattr(st, "basis", "") or "").strip()
            cid = getattr(st, "case_id", None)

            if cid is not None:
                if basis != "case":
                    out.append(f"{i}번 스텝: case_id가 있으면 basis는 'case'여야 한다(지금 '{basis}').")
                info = cases.get(str(cid))
                if info is None:
                    prev, bridged = None, []
                    continue
                if prev is not None:
                    ans = self.path(prev[0], prev[1], info["scene"], info.get("guard", {}))
                    if ans.kind != "SAME" and not bridged:
                        out.append(
                            f"{i}번 스텝: 앞 스텝에서 상태가 바뀌어야 하는데"
                            f"({prev[0]} → {info['scene']}) 사이에 아무 스텝도 없다. "
                            f"find_path로 물어 브리지 스텝을 넣거나, 모르면 basis 'unknown:'으로 적을 것.")
                    for b in bridged:
                        if b.startswith("unknown:") and ans.kind == "KNOWN":
                            out.append(
                                f"{i}번 앞 브리지: 'unknown'이라고 했지만 명세는 그 길을 안다"
                                f"({', '.join(ans.edges)}). find_path를 부르고 그 basis를 쓸 것.")
                prev = (info["scene"], {**info.get("guard", {}), **info.get("effect", {})})
                bridged = []
                continue

            # 브리지
            if not (basis.startswith("edge:") or basis.startswith("unknown:")):
                out.append(f"{i}번 스텝: case_id가 없으면 basis는 'edge:…' 또는 'unknown:…'이어야 "
                           f"한다(지금 '{basis}').")
            elif basis.startswith("edge:"):
                ref = basis[5:]
                if ref not in known:
                    out.append(f"{i}번 스텝: 'edge:{ref}'는 씬 명세에 없는 간선이다. "
                               "find_path가 돌려준 값을 그대로 쓸 것.")
            bridged.append(basis)
        return out

    # ---- 고치기 ------------------------------------------------------------------

    def repair(self, steps: list, cases: dict, make_step) -> tuple[list, list[str]]:
        """계약 위반을 **지적하지 않고 직접 고친다.**

        지적해서 다시 쓰게 하는 것은 실측에서 통하지 않았다(해소 0/3, 위반이 늘어난 경우 2/3).
        무엇을 넣을지가 `path()` 출력으로 정해져 있으므로 고르는 일이 없고, 고르는 일이 없으면
        모델을 부를 이유도 없다.

        네 가지를 고친다. 전부 결정적이다.
          · `case_id`가 있는 스텝의 `basis`는 정의상 `case`다 — 덮어쓴다
          · 상태가 바뀌는 자리가 비었으면 `path()`가 낸 스텝을 **끼워 넣는다**
          · 사람이 쓴 브리지는 문장을 살리고 `basis`만 계산값으로 바꾼다
          · 길을 모르면 `unknown:` 브리지를 넣는다 — 빈 자리로 두지 않는다

        **고치지 않는 것은 케이스의 순서다.** 그건 판단이고, 판단은 여기서 하지 않는다.
        """
        out: list = []
        changes: list[str] = []
        prev = None
        held: list = []          # 아직 내보내지 않은 브리지들

        def flush_gap(ans):
            """빈 자리를 메운다. 사람이 쓴 브리지가 있으면 문장을 살리고 basis만 고친다."""
            if held:
                for i, b in enumerate(held):
                    want = (f"edge:{ans.edges[i]}" if i < len(ans.edges)
                            else f"unknown:{ans.blocked_by}" if ans.kind == "UNKNOWN"
                            else f"edge:{ans.edges[-1]}" if ans.edges else "unknown:미상")
                    if getattr(b, "basis", None) != want:
                        changes.append(f"브리지 basis 교정 → {want}")
                        b.basis = want
                    out.append(b)
                return
            for text, eid in zip(ans.steps, ans.edges):
                changes.append(f"브리지 삽입 → edge:{eid}")
                out.append(make_step(text, f"edge:{eid}"))
            if ans.kind == "UNKNOWN":
                changes.append(f"모름 브리지 삽입 → {ans.blocked_by}")
                out.append(make_step(
                    f"{ans.blocked_by} 구간을 지나야 하는데 방법이 명세에 없다.",
                    f"unknown:{ans.blocked_by}"))

        for st in steps:
            if getattr(st, "case_id", None) is None:
                held.append(st)
                continue
            if getattr(st, "basis", None) != "case":
                changes.append(f"case {st.case_id} basis → case")
                st.basis = "case"
            info = cases.get(str(st.case_id))
            if info is not None and prev is not None:
                ans = self.path(prev[0], prev[1], info["scene"], info.get("guard", {}))
                if ans.kind == "SAME":
                    out.extend(held)
                else:
                    flush_gap(ans)
            else:
                out.extend(held)
            held = []
            out.append(st)
            prev = ((info["scene"], {**info.get("guard", {}), **info.get("effect", {})})
                    if info else None)
        out.extend(held)
        return out, changes
