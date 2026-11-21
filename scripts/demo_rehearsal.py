"""排练排页端到端演示（task 726 的离线排练扩展）。

用目录里真实的稳定页身份与摘录构造一场排练，并展示题目要求的全部情形：

1. 基础例：p,q,r,s 依次在用；可前翻一面可演（距离 1），禁翻则无解；
   新目录修订把 r 移到 p 后同面，禁翻也能全程零移动，旧版仍无解。
2. 范围端反转、休止空拍、正反翻速不同。
3. 整句两本替代谱本：只有一本能演；第一道间隙禁翻则两本都走不完，
   不能拼甲首拍与乙末拍；只改台型或目录修订可救活原来不能演的选本。
4. 多声部共享同一份卷目录与展开缓存。
5. 封存后在"断网台"上原样复核（哈希 + 重放），并演示篡改被发现。

运行：``python3 scripts/demo_rehearsal.py``
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system.catalog import Catalog  # noqa: E402
from citation_system.rehearsal.expansion import ExpansionEngine  # noqa: E402
from citation_system.rehearsal.model import (  # noqa: E402
    STAND_LEADING_BLANK,
    PageRecord,
    VolumeSnapshot,
    layout_from_snapshot,
    snapshot_from_catalog,
)
from citation_system.rehearsal.seal import (  # noqa: E402
    citation_registry_from_repo,
    seal_rehearsal,
    verify_bundle,
)
from citation_system.rehearsal.solver import solve_rehearsal  # noqa: E402
from citation_system.repository import CitationRepository  # noqa: E402


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(part: dict) -> None:
    if part["feasible"]:
        editions = ", ".join(
            f"{e['sentence_id']}={e['edition_id']}"
            for e in part["sentence_editions"]
        ) or "无句子"
        print(
            f"  [{part['part_id']}] 可演  最少翻动={part['total_distance']}  "
            f"逐拍面号={part['faces']}  移动={part['moves']}  选本({editions})"
        )
    else:
        f = part["failure"]
        print(
            f"  [{part['part_id']}] 无解  最早失败拍={f['beat']}  原因={f['reason']}  "
            f"当时可达面={f['reachable_openings']}  要求面={f['required_openings']}"
        )


# ----------------------------------------------------------------------
# 场景 1：基础例 + 目录修订
# ----------------------------------------------------------------------
def scenario_basic_and_revision() -> None:
    hr("场景1 基础例：p,q,r,s；可翻一面 vs 禁翻；目录修订后零移动")
    specs = [(x, "body", x, True) for x in "pqrs"]
    records = [
        PageRecord("V1", pid, sec, label, i, active)
        for i, (pid, sec, label, active) in enumerate(specs)
    ]
    pages = {p.as_key(): p for p in records}
    snap = VolumeSnapshot("V1", pages, tuple(records), {}, catalog_revision=1)
    layout_from_snapshot(snap, "old")
    # 新修订：把 r 移到 p 之前，于是 r 与 p 同处面1。
    layout_from_snapshot(snap, "new", active_page_ids=["r", "p", "q", "s"])
    print("  旧修订对开:", [[p.page_id for p in o.pages] for o in snap.layouts["old"]])
    print("  新修订对开:", [[p.page_id for p in o.pages] for o in snap.layouts["new"]])

    beats = [
        {"beat": 1, "refs": [{"page_id": "p"}]},
        {"beat": 2, "refs": [{"page_id": "r"}]},
    ]
    eng = ExpansionEngine({"V1": snap}, {})
    for revision, gap in (
        ("old", {"after_beat": 1, "forward": 1, "backward": 1}),
        ("old", {"after_beat": 1, "blocked": True}),
        ("new", {"after_beat": 1, "blocked": True}),
    ):
        plan = {
            "rehearsal_id": f"basic-{revision}",
            "volume_id": "V1",
            "revision_id": revision,
            "beats": beats,
            "parts": [{"part_id": "vn", "initial_opening": 1, "gaps": [gap]}],
        }
        label = "禁翻" if gap.get("blocked") else "可翻1面"
        print(f"  -- 修订={revision} {label}")
        show(solve_rehearsal(eng, "V1", revision, plan)["parts"][0])


# ----------------------------------------------------------------------
# 场景 2：范围反转、休止空拍、正反翻速不同
# ----------------------------------------------------------------------
def scenario_range_rest_asymmetric() -> None:
    hr("场景2 范围端反转 / 休止空拍 / 正反翻速不同")
    specs = [
        ("p1", "body", "1", True),
        ("p2", "body", "2", True),
        ("p3", "body", "3", True),
        ("p4", "body", "4", True),
    ]
    records = [
        PageRecord("V1", pid, sec, label, i, active)
        for i, (pid, sec, label, active) in enumerate(specs)
    ]
    pages = {p.as_key(): p for p in records}
    snap = VolumeSnapshot("V1", pages, tuple(records), {}, catalog_revision=1)
    layout_from_snapshot(snap, "default")
    citations = {
        # 端反转：start=p4 在 end=p2 之后。
        "CREV": {
            "citation_id": "CREV", "kind": "RANGE", "volume_id": "V1",
            "start_page_id": "p4", "end_page_id": "p2", "status": "pending",
        },
        "COK": {
            "citation_id": "COK", "kind": "RANGE", "volume_id": "V1",
            "start_page_id": "p1", "end_page_id": "p4", "status": "usable",
        },
    }
    eng = ExpansionEngine({"V1": snap}, citations)
    rev = eng.expand_beat("V1", "default", 1, [{"citation_id": "CREV"}])
    print("  反转范围状态:", rev.status, "| 来源顺序不交换:",
          [k[1] for k in rev.page_keys])
    ok = eng.expand_ref("V1", "default", {"citation_id": "COK"})
    print("  正常范围展开页:", [p.page_id for p in ok.pages],
          "对开:", ok.opening_nos)

    # 空拍 + 非对称翻速：面1=p1p2，面2=p3p4。前快后慢。
    plan = {
        "rehearsal_id": "rest-asym", "volume_id": "V1", "revision_id": "default",
        "beats": [
            {"beat": 1, "refs": [{"page_id": "p1"}]},
            {"beat": 2, "refs": []},  # 空拍
            {"beat": 3, "refs": [{"page_id": "p3"}]},
        ],
        "parts": [
            {
                "part_id": "forward-fast", "initial_opening": 1,
                # 间隙1 前翻1/后翻0；间隙2 前翻0/后翻2（向后更快）。
                "gaps": [
                    {"after_beat": 1, "forward": 1, "backward": 0},
                    {"after_beat": 2, "forward": 0, "backward": 2},
                ],
            }
        ],
    }
    show(solve_rehearsal(eng, "V1", "default", plan)["parts"][0])

    # 同安排但间隙1完全禁前翻：空拍也无法前移，第3拍不可达。
    plan_blocked = json.loads(json.dumps(plan))
    plan_blocked["parts"][0]["part_id"] = "blocked"
    plan_blocked["parts"][0]["gaps"][0] = {"after_beat": 1, "blocked": True}
    show(solve_rehearsal(eng, "V1", "default", plan_blocked)["parts"][0])


# ----------------------------------------------------------------------
# 场景 3：整句替代谱本 + 台型/目录修订救活
# ----------------------------------------------------------------------
def scenario_sentence_editions() -> None:
    hr("场景3 整句两本替代谱本；禁翻两本皆死；改台型/目录救活")
    specs = [(f"p{i}", "body", str(i), True) for i in range(1, 7)]
    records = [
        PageRecord("V1", pid, sec, label, i, active)
        for i, (pid, sec, label, active) in enumerate(specs)
    ]
    pages = {p.as_key(): p for p in records}

    citations = {
        "C1": _pin("C1", "p1"),
        "CA2": _pin("CA2", "p1"), "CA3": _pin("CA3", "p5"),
        "CB2": _pin("CB2", "p3"), "CB3": _pin("CB3", "p3"),
    }
    beats = [
        {"beat": 1, "refs": [{"citation_id": "C1"}]},
        {"beat": 2, "sentence_id": "S1", "alternatives": {
            "A": {"refs": [{"citation_id": "CA2"}]},
            "B": {"refs": [{"citation_id": "CB2"}]},
        }},
        {"beat": 3, "sentence_id": "S1", "alternatives": {
            "A": {"refs": [{"citation_id": "CA3"}]},
            "B": {"refs": [{"citation_id": "CB3"}]},
        }},
    ]
    gaps_ok = [
        {"after_beat": 1, "forward": 1, "backward": 1},
        {"after_beat": 2, "forward": 1, "backward": 1},
    ]

    # 默认台型（paired）：面1=p1p2 面2=p3p4 面3=p5p6。
    snap = VolumeSnapshot("V1", pages, tuple(records), {}, catalog_revision=1)
    layout_from_snapshot(snap, "paired")
    eng = ExpansionEngine({"V1": snap}, citations)
    plan = _sentence_plan("paired", gaps_ok, initial=1)
    print("  -- 默认台型，两间隙各可翻1面")
    show(solve_rehearsal(eng, "V1", "paired", plan)["parts"][0])

    # 第一道间隙禁翻：两本都走不完。
    gaps_blocked = [
        {"after_beat": 1, "blocked": True},
        {"after_beat": 2, "forward": 1, "backward": 1},
    ]
    plan_blocked = _sentence_plan("paired", gaps_blocked, initial=1)
    print("  -- 第一道间隙禁翻（不得拼甲首拍+乙末拍）")
    part = solve_rehearsal(eng, "V1", "paired", plan_blocked)["parts"][0]
    show(part)

    # 只改台型：卷首空位，p1 独占面2、p3 在面3；初始面改为 2。
    snap_blank = VolumeSnapshot("V1", pages, tuple(records), {}, catalog_revision=1)
    layout_from_snapshot(snap_blank, "blank", stand_type=STAND_LEADING_BLANK)
    eng_blank = ExpansionEngine({"V1": snap_blank}, citations)
    plan_blank = _sentence_plan("blank", gaps_ok, initial=2)
    print("  -- 只改台型 leading_blank（初始面2=p1）")
    show(solve_rehearsal(eng_blank, "V1", "blank", plan_blank)["parts"][0])

    # 只改目录修订：把 p3 移到 p1 同面，禁翻下乙也能零移动。
    snap_rev = VolumeSnapshot("V1", pages, tuple(records), {}, catalog_revision=2)
    layout_from_snapshot(
        snap_rev, "revised",
        active_page_ids=["p3", "p1", "p2", "p4", "p5", "p6"],
    )
    eng_rev = ExpansionEngine({"V1": snap_rev}, citations)
    plan_rev = _sentence_plan("revised", gaps_blocked, initial=1)
    print("  -- 只改目录修订（p3 与 p1 同面），第一道仍禁翻")
    show(solve_rehearsal(eng_rev, "V1", "revised", plan_rev)["parts"][0])


def _pin(cid: str, pid: str) -> dict:
    return {
        "citation_id": cid, "kind": "PINNED", "volume_id": "V1",
        "members": [{"volume_id": "V1", "page_id": pid}], "status": "usable",
    }


def _sentence_plan(revision: str, gaps: list, *, initial: int) -> dict:
    return {
        "rehearsal_id": f"sentence-{revision}",
        "volume_id": "V1",
        "revision_id": revision,
        "beats": [
            {"beat": 1, "refs": [{"citation_id": "C1"}]},
            {"beat": 2, "sentence_id": "S1", "alternatives": {
                "A": {"refs": [{"citation_id": "CA2"}]},
                "B": {"refs": [{"citation_id": "CB2"}]},
            }},
            {"beat": 3, "sentence_id": "S1", "alternatives": {
                "A": {"refs": [{"citation_id": "CA3"}]},
                "B": {"refs": [{"citation_id": "CB3"}]},
            }},
        ],
        "parts": [{"part_id": "vn", "initial_opening": initial, "gaps": gaps}],
    }


# ----------------------------------------------------------------------
# 场景 4/5：多声部共享 + 真实库 + 封存离线复核
# ----------------------------------------------------------------------
def scenario_seal_and_offline() -> None:
    hr("场景4/5 真实库多声部共享展开；封存后断网复核与篡改检测")
    tmp = Path(tempfile.mkdtemp(prefix="rehearsal-demo-"))
    try:
        cat = Catalog.create(str(tmp / "cat.db"))
        cat.load_pages_document({
            "format": "volume-pages/1",
            "revision": 1,
            "volumes": [{
                "volume_id": "V1",
                "pages": [
                    {"page_id": f"p{i}", "section_id": "body",
                     "label": str(i), "active": True}
                    for i in range(1, 7)
                ],
            }],
        })
        repo = CitationRepository(cat)
        for cid, pid in [("C1", "p1"), ("CA2", "p1"), ("CA3", "p5"),
                         ("CB2", "p3"), ("CB3", "p3")]:
            repo.create_pinned(cid, cid, "V1", [pid])

        snapshots = snapshot_from_catalog(cat)
        layout_from_snapshot(snapshots["V1"], "default")
        citations = citation_registry_from_repo(cat, repo)
        cat.close()

        eng = ExpansionEngine(snapshots, citations)
        plan = _sentence_plan("default", [
            {"after_beat": 1, "forward": 1, "backward": 1},
            {"after_beat": 2, "forward": 1, "backward": 1},
        ], initial=1)
        # 两个声部共用卷目录，选不同休止节奏（这里第二声部初始面不同）。
        plan["parts"] = [
            {"part_id": "vn-1", "initial_opening": 1, "gaps": [
                {"after_beat": 1, "forward": 1, "backward": 1},
                {"after_beat": 2, "forward": 1, "backward": 1},
            ]},
            # 同一卷目录、同一组展开；第二声部选更宽的翻页节奏（每间隙2面），
            # 因此甲、乙两本都在可行域内，求解器仍选全场翻动最小的一本。
            {"part_id": "vn-2", "initial_opening": 1, "gaps": [
                {"after_beat": 1, "forward": 2, "backward": 2},
                {"after_beat": 2, "forward": 2, "backward": 2},
            ]},
        ]
        solution = solve_rehearsal(eng, "V1", "default", plan)
        for part in solution["parts"]:
            show(part)
        print("  按版共享的展开缓存:", solution["expansion_cache"])

        bundle_dir = ROOT / "exports" / "rehearsal" / "sealed_bundle"
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        info = seal_rehearsal(
            bundle_dir, snapshots, citations, plan, solution=solution
        )
        print("  已封存:", info["bundle_file"])
        print("  SHA-256:", info["sha256"])

        verification = verify_bundle(bundle_dir)
        print("  断网复核: hash_ok=%s replay_ok=%s ok=%s"
              % (verification.hash_ok, verification.replay_ok, verification.ok))

        # 演示篡改：改封存结果后复核必须失败。
        bundle_path = bundle_dir / "bundle.json"
        doc = json.loads(bundle_path.read_text(encoding="utf-8"))
        doc["sealed_solution"]["parts"][0]["faces"] = [9, 9, 9]
        bundle_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        tampered = verify_bundle(bundle_dir)
        print("  篡改后复核: hash_ok=%s replay_ok=%s ok=%s（应全部 False）"
              % (tampered.hash_ok, tampered.replay_ok, tampered.ok))

        # 恢复一份干净、可在断网台复核的封存包作为交付物。
        shutil.rmtree(bundle_dir)
        clean = seal_rehearsal(
            bundle_dir, snapshots, citations, plan, solution=solution
        )
        clean_check = verify_bundle(bundle_dir)
        print("  已恢复干净封存包，再次复核 ok=%s sha256=%s"
              % (clean_check.ok, clean["sha256"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    scenario_basic_and_revision()
    scenario_range_rest_asymmetric()
    scenario_sentence_editions()
    scenario_seal_and_offline()
    print("\n全部场景演示完成。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
