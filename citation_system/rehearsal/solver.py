"""排练排页求解：最少实际翻动距离与逐拍面号。

模型
----

* 台有 ``N`` 个对开（面），编号 ``1..N``。声部给定**起始对开**，第 1 拍
  必须落在该面。
* 每个间隙（第 ``i`` 拍与第 ``i+1`` 拍之间）给定正、反翻页速率：一次间隙
  至多向前（面号增大）移动 ``forward`` 面、向后（面号减小）移动
  ``backward`` 面；两者皆 0 即**禁翻间隙**，只能保持原面。
* 每拍的要求是一个允许面集合（通常单元素）；**空拍**（无任何引用）不要求
  显示内容，允许停在任意面，但仍受间隙速率约束。
* 成本是实际翻动距离，即各间隙面号差的绝对值之和；原地不动成本为 0。

求解用逐拍动态规划：``dp[拍][面] = 到达该面的最小累计距离``，并保留父指针
以还原逐拍面号。同成本时取面号较小者，保证输出确定（题目允许任一合法排法）。

整句替代谱本
------------

一句（连续数拍）有两份经过批准的替代谱本，同一句必须从头到尾用同一本，
只在句间改本，台面位置承接上一句。实现上把整场切成若干**段**：普通段只有
一本，句子段对每本各跑一次段内 DP，再在段边界按出口面取较优者，并记下
胜出的版本。某本在句内任一拍结构性失败（待核/跨卷/不能同显）即判该本死亡；
若句中所有本都死亡，给出"不晚于哪一拍所有选择都已失效"
（各本最早失败拍的最大值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .expansion import (
    OK,
    ExpansionEngine,
)


@dataclass(frozen=True)
class Gap:
    """一拍间隙的翻页许可。``(0, 0)`` 表示禁翻。"""

    after_beat: int
    forward: int
    backward: int

    @property
    def blocked(self) -> bool:
        return self.forward == 0 and self.backward == 0

    def allows(self, delta: int) -> bool:
        if delta == 0:
            return True
        if delta > 0:
            return delta <= self.forward
        return -delta <= self.backward

    def as_dict(self) -> dict[str, Any]:
        return {
            "after_beat": self.after_beat,
            "forward": self.forward,
            "backward": self.backward,
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class BeatRequirement:
    beat: int
    required: frozenset[int] | None  # None = 空拍，任意面
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_rest(self) -> bool:
        return self.required is None


@dataclass(frozen=True)
class Failure:
    beat: int
    reason: str
    reachable_openings: tuple[int, ...]
    required_openings: tuple[int, ...] | None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "beat": self.beat,
            "reason": self.reason,
            "reachable_openings": list(self.reachable_openings),
            "required_openings": (
                list(self.required_openings)
                if self.required_openings is not None
                else None
            ),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PartResult:
    part_id: str
    feasible: bool
    total_distance: int | None
    faces: tuple[int, ...]          # 逐拍面号，无解时为到失败前的部分
    gaps: tuple[Gap, ...]
    moves: tuple[int, ...]          # 各间隙的实际移动（带符号）
    failure: Failure | None
    sentence_editions: tuple[tuple[str, str], ...]  # (sentence_id, edition_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "feasible": self.feasible,
            "total_distance": self.total_distance,
            "faces": list(self.faces),
            "moves": list(self.moves),
            "gaps": [g.as_dict() for g in self.gaps],
            "failure": None if self.failure is None else self.failure.as_dict(),
            "sentence_editions": [
                {"sentence_id": s, "edition_id": e}
                for s, e in self.sentence_editions
            ],
        }


# ----------------------------------------------------------------------
# 单本逐拍 DP
# ----------------------------------------------------------------------
def _apply_requirement(
    beat: int,
    costs: dict[int, int],
    requirement: BeatRequirement,
) -> tuple[dict[int, int], Failure | None]:
    """把一拍的显示要求作用到可达面集合上。"""

    if requirement.status != OK:
        return {}, Failure(
            beat=beat,
            reason=requirement.status,
            reachable_openings=tuple(sorted(costs)),
            required_openings=None,
            detail=requirement.detail,
        )
    if requirement.is_rest:
        return costs, None
    allowed = {o: c for o, c in costs.items() if o in requirement.required}
    if allowed:
        return allowed, None
    return {}, Failure(
        beat=beat,
        reason="unreachable_required_opening",
        reachable_openings=tuple(sorted(costs)),
        required_openings=tuple(sorted(requirement.required)),  # type: ignore[arg-type]
        detail={},
    )


def solve_part(
    requirements: list[BeatRequirement],
    initial_opening: int,
    gaps: list[Gap],
    opening_count: int,
) -> PartResult:
    """单一声部、单本谱的逐拍 DP。"""

    if not requirements:
        raise ValueError("a rehearsal needs at least one beat")
    if not 1 <= initial_opening <= opening_count:
        raise ValueError("initial_opening outside the stand")
    if len(gaps) != len(requirements) - 1:
        raise ValueError("one gap required between every pair of adjacent beats")

    # parents[beat_index][opening] = (prev_opening, cost)
    parents: list[dict[int, tuple[int, int]]] = [{} for _ in requirements]
    costs: dict[int, int] = {initial_opening: 0}
    first = requirements[0]
    costs, failure = _apply_requirement(1, costs, first)
    if failure is not None:
        return PartResult(
            part_id="part", feasible=False, total_distance=None, faces=(),
            gaps=tuple(gaps), moves=(), failure=failure, sentence_editions=(),
        )

    all_openings = range(1, opening_count + 1)
    for i, gap in enumerate(gaps):
        next_beat = i + 2
        nxt: dict[int, tuple[int, int]] = {}
        for o_prev, cost_prev in sorted(costs.items()):
            for o_next in all_openings:
                delta = o_next - o_prev
                if not gap.allows(delta):
                    continue
                new_cost = cost_prev + abs(delta)
                old = nxt.get(o_next)
                # 同成本取较小来源面，保证确定性。
                if old is None or new_cost < old[1] or (
                    new_cost == old[1] and o_prev < old[0]
                ):
                    nxt[o_next] = (o_prev, new_cost)
        parents[i + 1] = nxt
        candidate_costs = {o: v[1] for o, v in nxt.items()}
        costs, failure = _apply_requirement(
            next_beat, candidate_costs, requirements[i + 1]
        )
        if failure is not None:
            # 回溯失败前已经确定的面（到上一拍为止）。
            known_face = min(
                nxt, key=lambda o: (nxt[o][1], o)
            ) if nxt else initial_opening
            known = _backtrack(parents, i, known_face,
                               initial=initial_opening, upto=i + 1)
            if not known:
                known = [initial_opening]
            return PartResult(
                part_id="part", feasible=False, total_distance=None,
                faces=tuple(known), gaps=tuple(gaps), moves=(),
                failure=failure, sentence_editions=(),
            )

    # 成功：取终拍成本最小（同成本面号最小）的面回溯。
    end_opening = min(costs, key=lambda o: (costs[o], o))
    faces = _backtrack(parents, len(requirements) - 1, end_opening,
                       initial_opening, len(requirements))
    moves = tuple(faces[i + 1] - faces[i] for i in range(len(faces) - 1))
    return PartResult(
        part_id="part", feasible=True, total_distance=costs[end_opening],
        faces=tuple(faces), gaps=tuple(gaps), moves=moves,
        failure=None, sentence_editions=(),
    )


def _backtrack(
    parents: list[dict[int, tuple[int, int]]],
    last_index: int,
    end_opening: int | None,
    initial: int,
    upto: int,
) -> list[int]:
    """从 ``last_index`` 拍回溯到第 1 拍。"""

    if last_index == 0:
        return [initial]
    if end_opening is None:
        return []
    chain = [end_opening]
    cur = end_opening
    for idx in range(last_index, 0, -1):
        cur = parents[idx][cur][0]
        chain.append(cur)
    chain.reverse()
    return chain[:upto]


# ----------------------------------------------------------------------
# 整场：段（普通段 / 句子段）+ 多声部
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class _Segment:
    start_beat: int                 # 1-based，含
    end_beat: int                   # 1-based，含
    sentence_id: str | None
    editions: tuple[str, ...]       # 普通段只有一个占位版本 ""


def _build_segments(beats: list[dict[str, Any]]) -> list[_Segment]:
    # 先校验：同一个 sentence_id 的拍子必须构成一段连续区间，且整句各拍的
    # 替代版本集合完全一致（同句必须能从头到尾用同一本）。
    seen_sentences: dict[str, tuple[int, tuple[str, ...]]] = {}
    for index, beat in enumerate(beats, 1):
        if "sentence_id" not in beat:
            continue
        sid = beat["sentence_id"]
        editions = tuple(sorted(beat.get("alternatives", {}).keys()))
        if not editions:
            raise ValueError(f"sentence {sid!r} at beat {index} has no editions")
        if sid in seen_sentences:
            prev_end, prev_editions = seen_sentences[sid]
            if prev_end != index - 1:
                raise ValueError(
                    f"sentence {sid!r} is interrupted by another sentence; "
                    "a sentence must be one contiguous beat range"
                )
            if prev_editions != editions:
                raise ValueError(
                    f"sentence {sid!r} changes its edition set between beats; "
                    "all beats of a sentence must list the same editions"
                )
            seen_sentences[sid] = (index, editions)
        else:
            seen_sentences[sid] = (index, editions)

    segments: list[_Segment] = []
    i = 0
    while i < len(beats):
        beat_no = i + 1
        beat = beats[i]
        if "sentence_id" not in beat:
            segments.append(_Segment(beat_no, beat_no, None, ("",)))
            i += 1
            continue
        sid = beat["sentence_id"]
        editions = seen_sentences[sid][1]
        j = i
        while (
            j < len(beats)
            and beats[j].get("sentence_id") == sid
        ):
            j += 1
        segments.append(_Segment(beat_no, j, sid, editions))
        i = j
    # 段边界必须严丝合缝（覆盖每一拍，不重叠）。
    expected = 1
    for seg in segments:
        if seg.start_beat != expected:
            raise ValueError(
                f"segments overlap or leave a gap near beat {expected}"
            )
        expected = seg.end_beat + 1
    return segments


def parse_gaps(doc: list[dict[str, Any]], beat_count: int) -> list[Gap]:
    by_beat = {int(g["after_beat"]): g for g in doc}
    gaps: list[Gap] = []
    for after in range(1, beat_count):
        if after not in by_beat:
            raise ValueError(f"missing turn rule for gap after beat {after}")
        g = by_beat[after]
        if g.get("blocked"):
            forward, backward = 0, 0
        else:
            forward = int(g.get("forward", 0))
            backward = int(g.get("backward", 0))
        if forward < 0 or backward < 0:
            raise ValueError("turn rates must be non-negative")
        gaps.append(Gap(after_beat=after, forward=forward, backward=backward))
    return gaps


def _requirement_from_beat(
    engine: ExpansionEngine,
    volume_id: str,
    revision_id: str,
    beat_no: int,
    refs: list[dict[str, Any]],
) -> BeatRequirement:
    if not refs:
        return BeatRequirement(beat=beat_no, required=None, status=OK)
    expansion = engine.expand_beat(volume_id, revision_id, beat_no, refs)
    if expansion.status == OK:
        return BeatRequirement(
            beat=beat_no,
            required=frozenset({expansion.required_opening_no}),
            status=OK,
        )
    detail = {"expansion": expansion.as_dict()}
    return BeatRequirement(
        beat=beat_no, required=None, status=expansion.status, detail=detail
    )


@dataclass
class _EditionRun:
    """一个段内某版本的逐拍要求与（可能的）最早失败。"""

    edition_id: str
    requirements: list[BeatRequirement]
    earliest_failure: Failure | None


def _run_segment_editions(
    engine: ExpansionEngine,
    volume_id: str,
    revision_id: str,
    segment: _Segment,
    beats: list[dict[str, Any]],
) -> list[_EditionRun]:
    runs: list[_EditionRun] = []
    for edition_id in segment.editions:
        reqs: list[BeatRequirement] = []
        earliest: Failure | None = None
        for offset in range(segment.end_beat - segment.start_beat + 1):
            beat_no = segment.start_beat + offset
            beat = beats[beat_no - 1]
            if segment.sentence_id is None:
                refs = beat.get("refs", [])
            else:
                refs = beat["alternatives"][edition_id].get("refs", [])
            req = _requirement_from_beat(
                engine, volume_id, revision_id, beat_no, refs
            )
            reqs.append(req)
            if req.status != OK and earliest is None:
                earliest = Failure(
                    beat=beat_no,
                    reason=req.status,
                    reachable_openings=(),
                    required_openings=None,
                    detail=req.detail,
                )
        runs.append(_EditionRun(edition_id, reqs, earliest))
    return runs


def solve_rehearsal(
    engine: ExpansionEngine,
    volume_id: str,
    revision_id: str,
    rehearsal: dict[str, Any],
) -> dict[str, Any]:
    """解一场排练的所有声部。

    展开结果按（卷、修订、引用）缓存于 ``engine``，因此多声部、多套休止
    安排共享同一份展开。
    """

    beats = rehearsal["beats"]
    beat_count = len(beats)
    segments = _build_segments(beats)
    opening_count = engine.snapshots[volume_id].opening_count(revision_id)

    # 先把每个段的每个版本都展开一遍：死版本在 DP 前就分清，并给出
    # "所有选择均已失效不晚于哪一拍"。
    segment_runs: list[list[_EditionRun]] = []
    sentence_failure: Failure | None = None
    for segment in segments:
        runs = _run_segment_editions(
            engine, volume_id, revision_id, segment, beats
        )
        segment_runs.append(runs)
        if segment.sentence_id is not None:
            dead = [r for r in runs if r.earliest_failure is not None]
            if len(dead) == len(runs):
                worst = max(dead, key=lambda r: r.earliest_failure.beat)
                sentence_failure = Failure(
                    beat=worst.earliest_failure.beat,
                    reason="all_edition_choices_failed",
                    reachable_openings=(),
                    required_openings=None,
                    detail={
                        "sentence_id": segment.sentence_id,
                        "edition_failures": [
                            {
                                "edition_id": r.edition_id,
                                "beat": r.earliest_failure.beat,
                                "reason": r.earliest_failure.reason,
                            }
                            for r in sorted(
                                dead, key=lambda r: r.earliest_failure.beat
                            )
                        ],
                    },
                )
                break

    parts_out: list[PartResult] = []
    for part in rehearsal["parts"]:
        if sentence_failure is not None:
            parts_out.append(PartResult(
                part_id=part["part_id"], feasible=False, total_distance=None,
                faces=(), gaps=(), moves=(), failure=sentence_failure,
                sentence_editions=(),
            ))
            continue
        result = _solve_part_segments(
            part, segments, segment_runs, opening_count
        )
        parts_out.append(result)

    return {
        "rehearsal_id": rehearsal.get("rehearsal_id"),
        "volume_id": volume_id,
        "revision_id": revision_id,
        "beat_count": beat_count,
        "opening_count": opening_count,
        "parts": [p.as_dict() for p in parts_out],
        "expansion_cache": engine.cache_identity(volume_id, revision_id),
    }




def _solve_part_segments(
    part: dict[str, Any],
    segments: list[_Segment],
    segment_runs: list[list[_EditionRun]],
    opening_count: int,
) -> PartResult:
    """逐拍全局 DP，段只决定"该拍用哪本的要求"。

    对每个段、每个可选版本，把该版本在段内各拍的允许面要求作用到同一张
    全局成本图上，并在**每一拍**保留父指针 ``(来面, 成本, 版本)``。这样
    回溯始终在拍层进行，段与段之间不会有任何手工拼接：句子内各拍版本必然
    一致（同一段只遍历一个版本的要求序列），句间才在段边界比较各版本出口。
    """

    beat_count = max(s.end_beat for s in segments)
    gaps = parse_gaps(part.get("gaps", []), beat_count)
    initial = int(part["initial_opening"])
    if not 1 <= initial <= opening_count:
        raise ValueError("initial_opening outside the stand")

    # 每拍每个面的最优状态：(成本, 来面, 版本)。第 1 拍来面为自身。
    parents: list[dict[int, tuple[int, int, str]]] = [
        {} for _ in range(beat_count)
    ]
    costs: dict[int, int] = {initial: 0}
    local_failure: Failure | None = None

    for seg, runs in zip(segments, segment_runs):
        # 段边界：对每个版本各跑一次段内 DP，再按出口面择优。我们让每个
        # 版本从同一份入口成本图出发，单独计算其段内父链，最后只把胜出
        # 版本在该段各拍的父链并入全局 parents。
        candidates: list[tuple[
            dict[int, int], list[dict[int, tuple[int, int, str]]], str
        ]] = []
        seg_failure: Failure | None = None
        for run in runs:
            if run.earliest_failure is not None:
                continue
            ok, seg_costs, seg_parents, failure = _run_one_edition(
                costs, run.requirements, gaps, opening_count,
                seg.start_beat, run.edition_id,
            )
            if not ok:
                if seg_failure is None or failure.beat < seg_failure.beat:
                    seg_failure = failure
                continue
            candidates.append((seg_costs, seg_parents, run.edition_id))
        if not candidates:
            reason = seg_failure.reason if seg_failure else "no_edition_reachable"
            local_failure = Failure(
                beat=(seg_failure.beat if seg_failure else seg.start_beat),
                reason=reason,
                reachable_openings=(
                    seg_failure.reachable_openings if seg_failure
                    else tuple(sorted(costs))
                ),
                required_openings=(
                    seg_failure.required_openings if seg_failure else None
                ),
                detail={
                    "sentence_id": seg.sentence_id,
                    **(seg_failure.detail if seg_failure else {}),
                },
            )
            break
        # 按出口面择优合并；同成本取版本号较小者，保证确定性。
        merged_costs: dict[int, int] = {}
        winner_by_exit: dict[int, int] = {}
        for idx, (seg_costs, _seg_parents, edition_id) in enumerate(candidates):
            for o_exit, cost in seg_costs.items():
                old_cost = merged_costs.get(o_exit)
                if old_cost is None or cost < old_cost or (
                    cost == old_cost and edition_id
                    < candidates[winner_by_exit[o_exit]][2]
                ):
                    merged_costs[o_exit] = cost
                    winner_by_exit[o_exit] = idx
        # 把每个出口胜出版本的段内父链并入全局。不同出口可能来自不同版本，
        # 因此按出口分别记录，回溯时沿具体来面取对应条目。
        for o_exit, idx in winner_by_exit.items():
            _seg_costs, seg_parents, edition_id = candidates[idx]
            for local_i, table in enumerate(seg_parents):
                global_i = (seg.start_beat - 1) + local_i
                for face, entry in table.items():
                    parents[global_i][face] = entry
        costs = merged_costs

    if local_failure is not None:
        return PartResult(
            part_id=part["part_id"], feasible=False, total_distance=None,
            faces=(), gaps=tuple(gaps), moves=(),
            failure=local_failure, sentence_editions=(),
        )

    end_face = min(costs, key=lambda o: (costs[o], o))
    total = costs[end_face]
    faces = [end_face]
    editions_used: list[str] = []
    cur = end_face
    for i in range(beat_count - 1, 0, -1):
        prev_face, _prev_cost, edition_id = parents[i][cur]
        if edition_id:
            editions_used.append(edition_id)
        cur = prev_face
        faces.append(cur)
    faces.reverse()
    moves = tuple(faces[i + 1] - faces[i] for i in range(beat_count - 1))

    # 句子选用的版本：取每个句子段内任一拍记录的版本（同段内一致）。
    sentence_editions: list[tuple[str, str]] = []
    for seg, runs in zip(segments, segment_runs):
        if seg.sentence_id is None:
            continue
        global_i = seg.start_beat - 1
        face = faces[global_i]
        _prev, _cost, edition_id = parents[global_i][face]
        sentence_editions.append((seg.sentence_id, edition_id))

    return PartResult(
        part_id=part["part_id"], feasible=True, total_distance=total,
        faces=tuple(faces), gaps=tuple(gaps), moves=moves,
        failure=None, sentence_editions=tuple(sentence_editions),
    )


def _run_one_edition(
    entry_costs: dict[int, int],
    requirements: list[BeatRequirement],
    gaps: list[Gap],
    opening_count: int,
    start_beat: int,
    edition_id: str,
) -> tuple[bool, dict[int, int], list[dict[int, tuple[int, int, str]]], Failure | None]:
    """一个版本在一个段内的逐拍 DP，父指针额外记下版本号。

    返回 (是否成功, 段末成本图, 段内各拍父链表, 失败)。父链表下标 0 对应
    全局第 start_beat 拍；其条目 ``(来面, 成本, 版本)`` 中"来面"在首拍即
    入口面本身（跨段间隙的移动已计入成本）。
    """

    seg_parents: list[dict[int, tuple[int, int, str]]] = [
        {} for _ in requirements
    ]
    costs = dict(entry_costs)

    # 进入段首拍：若不是整场第 1 拍，先经过上一拍之后的间隙。
    if start_beat != 1:
        entry_gap = gaps[start_beat - 2]
        moved: dict[int, tuple[int, int, str]] = {}
        for o_prev, cost_prev in sorted(entry_costs.items()):
            for o_next in range(1, opening_count + 1):
                delta = o_next - o_prev
                if not entry_gap.allows(delta):
                    continue
                new_cost = cost_prev + abs(delta)
                old = moved.get(o_next)
                if old is None or new_cost < old[1] or (
                    new_cost == old[1] and o_prev < old[0]
                ):
                    moved[o_next] = (o_prev, new_cost, edition_id)
        seg_parents[0] = moved
        costs = {o: v[1] for o, v in moved.items()}
    else:
        for o, cost in costs.items():
            seg_parents[0][o] = (o, cost, edition_id)

    costs, failure = _apply_requirement(start_beat, costs, requirements[0])
    if failure is not None:
        return False, {}, [], failure

    for i in range(len(requirements) - 1):
        # 段内第 i 个要求（全局第 start_beat+i 拍）之后的间隙；gaps[k] 位于
        # 第 k+1 拍之后，故下标为 (start_beat+i)-1。
        gap = gaps[(start_beat + i) - 1]
        next_beat = start_beat + i + 1
        nxt: dict[int, tuple[int, int, str]] = {}
        for o_prev, cost_prev in sorted(costs.items()):
            for o_next in range(1, opening_count + 1):
                delta = o_next - o_prev
                if not gap.allows(delta):
                    continue
                new_cost = cost_prev + abs(delta)
                old = nxt.get(o_next)
                if old is None or new_cost < old[1] or (
                    new_cost == old[1] and o_prev < old[0]
                ):
                    nxt[o_next] = (o_prev, new_cost, edition_id)
        seg_parents[i + 1] = nxt
        costs = {o: v[1] for o, v in nxt.items()}
        costs, failure = _apply_requirement(
            next_beat, costs, requirements[i + 1]
        )
        if failure is not None:
            return False, {}, [], failure

    return True, costs, seg_parents, None
