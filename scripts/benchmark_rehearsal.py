"""排练排页在目标规模下的性能基准。

目标：单场数百拍（默认 600 拍）、多个声部、多套休止安排，验证

* 展开按（卷、修订、引用）缓存，多声部只算一次；
* 逐拍 DP 与句子分段在数百拍上仍是亚秒级；
* 封存与断网重放的成本。
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))

from citation_system.rehearsal.expansion import ExpansionEngine  # noqa: E402
from citation_system.rehearsal.model import (  # noqa: E402
    PageRecord,
    VolumeSnapshot,
    layout_from_snapshot,
)
from citation_system.rehearsal.seal import seal_rehearsal, verify_bundle  # noqa: E402
from citation_system.rehearsal.solver import solve_rehearsal  # noqa: E402

RNG = random.Random(726)
BEATS = 600
PARTS = 4
OPENINGS = 40  # 80 个在用页，成对 => 40 面


def build_snapshot() -> VolumeSnapshot:
    records = [
        PageRecord("V1", f"p{i}", "body", str(i), i, True)
        for i in range(1, OPENINGS * 2 + 1)
    ]
    pages = {p.as_key(): p for p in records}
    snap = VolumeSnapshot("V1", pages, tuple(records), {}, catalog_revision=1)
    layout_from_snapshot(snap, "default")
    return snap


def build_plan() -> dict:
    # 谱页随排练渐进推进：每拍在当前面附近游走（真实排练很少一次跳整本），
    # 约 15% 的拍是空拍（休止）。这样有限翻速下大部分声部可行。
    beats = []
    current_page = 1
    for b in range(1, BEATS + 1):
        if RNG.random() < 0.15:
            beats.append({"beat": b, "refs": []})
            continue
        current_page = max(
            1, min(OPENINGS * 2, current_page + RNG.choice([-1, 0, 1, 1, 2]))
        )
        beats.append({
            "beat": b,
            "refs": [{"page_id": f"p{current_page}"}],
        })

    parts = []
    for k in range(PARTS):
        # 每套休止安排用不同的翻页速率（不同声部），共享同一份卷目录与展开。
        # 谱页每拍至多推进一个面，正反速率都至少给 1，保证渐进游走可跟上；
        # 取值分布不同即代表不同的休止/翻页安排。
        gaps = [
            {
                "after_beat": b,
                "forward": RNG.choice([1, 2, 2, 3]),
                "backward": RNG.choice([1, 1, 2, 3]),
            }
            for b in range(1, BEATS)
        ]
        parts.append({
            "part_id": f"part-{k}",
            # 各声部同从面 1 起，差异在翻页速率（不同休止/翻页安排）。
            "initial_opening": 1,
            "gaps": gaps,
        })
    return {
        "rehearsal_id": "scale-600",
        "volume_id": "V1",
        "revision_id": "default",
        "beats": beats,
        "parts": parts,
    }


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return time.perf_counter() - start, value


def main() -> int:
    snap = build_snapshot()
    plan = build_plan()
    distinct_refs = len({
        ref["page_id"]
        for beat in plan["beats"] for ref in beat.get("refs", [])
    })

    t_build, _ = timed(lambda: None)  # 快照已建，占位保持输出列齐
    t0 = time.perf_counter()
    engine = ExpansionEngine({"V1": snap}, {})
    t_engine = time.perf_counter() - t0

    t_solve, solution = timed(
        lambda: solve_rehearsal(engine, "V1", "default", plan)
    )
    feasible = sum(1 for p in solution["parts"] if p["feasible"])
    cached = solution["expansion_cache"]["cached_refs"]

    tmp = Path(tempfile.mkdtemp(prefix="rehearsal-bench-"))
    try:
        t_seal, info = timed(lambda: seal_rehearsal(
            tmp / "bundle", {"V1": snap}, {}, plan, solution=solution
        ))
        t_verify, verification = timed(lambda: verify_bundle(tmp / "bundle"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"拍数={BEATS} 声部数={PARTS} 台面数={OPENINGS} 不同引用={distinct_refs}")
    print(f"引擎构造:        {t_engine * 1000:8.2f} ms")
    print(f"整场求解(4声部): {t_solve * 1000:8.2f} ms  "
          f"可行声部={feasible}/{PARTS}  共享展开条目={cached}")
    print(f"封存离线包:      {t_seal * 1000:8.2f} ms  sha256={info['sha256'][:16]}…")
    print(f"断网复核(哈希+重放): {t_verify * 1000:8.2f} ms  ok={verification.ok}")
    return 0 if verification.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
