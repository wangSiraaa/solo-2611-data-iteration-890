"""排练排页（rehearsal paging）单元与集成测试。

覆盖：目录修订/台型成对、稳定身份与同号附页、引用展开的全部状态
（可用/待处理/跨卷/不能同显/解析歧义）、逐拍 DP 的最少翻动与无解诊断、
整句替代谱本的版本一致性、按版共享的展开缓存，以及封存包的断网复核与
篡改检测。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from citation_system.catalog import Catalog
from citation_system.rehearsal import seal as seal_mod
from citation_system.rehearsal.expansion import (
    CROSS_VOLUME,
    NOT_CO_DISPLAYABLE,
    OK,
    PENDING,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_NOT_FOUND,
    ExpansionEngine,
)
from citation_system.rehearsal.model import (
    STAND_LEADING_BLANK,
    PageRecord,
    VolumeSnapshot,
    layout_from_snapshot,
    snapshot_from_catalog,
    snapshot_from_document,
)
from citation_system.rehearsal.seal import seal_rehearsal, verify_bundle
from citation_system.rehearsal.solver import (
    Gap,
    solve_part,
    solve_rehearsal,
)
from citation_system.repository import CitationRepository

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# 纯内存快照夹具
# ----------------------------------------------------------------------
def make_snapshot(page_specs, volume_id="V1"):
    """page_specs: list of (page_id, section, label, active)，seq 按下标。"""

    records = [
        PageRecord(volume_id, pid, section, label, i, active)
        for i, (pid, section, label, active) in enumerate(page_specs)
    ]
    pages = {p.as_key(): p for p in records}
    return VolumeSnapshot(volume_id, pages, tuple(records), {}, catalog_revision=1)


def six_page_snapshot(stand="paired", revision="default", order=None):
    specs = [(f"p{i}", "body", str(i), True) for i in range(1, 7)]
    snap = make_snapshot(specs)
    if order is not None:
        layout_from_snapshot(snap, revision, active_page_ids=order)
    else:
        layout_from_snapshot(snap, revision, stand_type=stand)
    return snap


def pinned(cid, volume_id, page_ids, status="usable"):
    return {
        "citation_id": cid,
        "kind": "PINNED",
        "volume_id": volume_id,
        "members": [{"volume_id": volume_id, "page_id": p} for p in page_ids],
        "status": status,
    }


def range_citation(cid, volume_id, start, end, status="usable"):
    return {
        "citation_id": cid,
        "kind": "RANGE",
        "volume_id": volume_id,
        "start_page_id": start,
        "end_page_id": end,
        "status": status,
    }


class LayoutTests(unittest.TestCase):
    def test_paired_default_pairs_1_2_and_3_4(self):
        snap = six_page_snapshot()
        openings = snap.layouts["default"]
        self.assertEqual(
            [[p.page_id for p in o.pages] for o in openings],
            [["p1", "p2"], ["p3", "p4"], ["p5", "p6"]],
        )
        self.assertEqual([o.opening_no for o in openings], [1, 2, 3])

    def test_paired_odd_active_count_ends_with_single_page(self):
        specs = [(x, "body", x, True) for x in "pqrs"]
        snap = make_snapshot(specs)
        layout_from_snapshot(snap, "d")
        self.assertEqual(
            [[p.page_id for p in o.pages] for o in snap.layouts["d"]],
            [["p", "q"], ["r", "s"]],
        )
        specs.append(("t", "body", "t", True))
        snap = make_snapshot(specs)
        layout_from_snapshot(snap, "d")
        self.assertEqual(
            [[p.page_id for p in o.pages] for o in snap.layouts["d"]],
            [["p", "q"], ["r", "s"], ["t"]],
        )

    def test_leading_blank_stand_first_page_owns_first_face(self):
        snap = six_page_snapshot(stand=STAND_LEADING_BLANK)
        openings = snap.layouts["default"]
        self.assertTrue(openings[0].leading_blank)
        self.assertEqual(openings[0].pages, ())
        self.assertEqual([p.page_id for p in openings[1].pages], ["p1"])
        self.assertEqual(
            [p.page_id for p in openings[2].pages], ["p2", "p3"]
        )
        self.assertEqual(
            [p.page_id for p in openings[-1].pages], ["p6"]
        )

    def test_inactive_pages_occupy_no_opening_but_keep_identity(self):
        specs = [
            ("p1", "body", "1", True),
            ("p2", "body", "2", False),  # 停用，不占面
            ("p3", "body", "3", True),
        ]
        snap = make_snapshot(specs)
        layout_from_snapshot(snap, "d")
        self.assertEqual(
            [[p.page_id for p in o.pages] for o in snap.layouts["d"]],
            [["p1", "p3"]],
        )
        # 停用页身份与物理位置仍在快照里。
        self.assertFalse(snap.pages[("V1", "p2")].active)
        self.assertEqual(snap.pages[("V1", "p2")].seq, 1)
        self.assertIsNone(snap.opening_of("d", ("V1", "p2")))

    def test_same_printed_label_distinct_identities_never_confused(self):
        specs = [
            ("p3", "body", "3", True),
            ("pa", "appendix", "3", True),  # 同印刷号 3，不同 section/身份
        ]
        snap = make_snapshot(specs)
        layout_from_snapshot(snap, "d")
        self.assertEqual(
            [[p.page_id for p in o.pages] for o in snap.layouts["d"]],
            [["p3", "pa"]],
        )
        labels = snap.label_index()
        self.assertEqual(
            sorted(p.page_id for p in labels[("body", "3")]), ["p3"]
        )
        self.assertEqual(
            sorted(p.page_id for p in labels[("appendix", "3")]), ["pa"]
        )


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        # p3 停用；p4 是 appendix/3（与 body/3 同号附页）。
        specs = [
            ("p1", "body", "1", True),
            ("p2", "body", "2", True),
            ("p3", "body", "3", False),
            ("p4", "appendix", "3", True),
            ("p5", "body", "4", True),
        ]
        self.snap = make_snapshot(specs)
        layout_from_snapshot(self.snap, "rev1")
        self.snap_v2 = make_snapshot([("z", "body", "z", True)], "V2")

    def engine(self, citations):
        return ExpansionEngine(
            {"V1": self.snap, "V2": self.snap_v2}, citations
        )

    def test_pinned_with_inactive_member_is_pending_and_kept(self):
        eng = self.engine({"CP": pinned("CP", "V1", ["p2", "p3"], PENDING)})
        result = eng.expand_beat("V1", "rev1", 1, [{"citation_id": "CP"}])
        self.assertEqual(result.status, PENDING)
        # 停用成员不被静默删除。
        self.assertEqual(
            sorted(k[1] for k in result.page_keys), ["p2", "p3"]
        )

    def test_range_includes_active_pages_and_excludes_inactive(self):
        eng = self.engine(
            {"CR": range_citation("CR", "V1", "p2", "p5")}
        )
        result = eng.expand_ref("V1", "rev1", {"citation_id": "CR"})
        self.assertEqual(result.status, OK)
        self.assertEqual(
            [p.page_id for p in result.pages], ["p2", "p4", "p5"]
        )
        roles = {p.page_id: x.role for p, x in zip(result.pages, result.provenance)}
        self.assertEqual(roles["p2"], "range_start")
        self.assertEqual(roles["p4"], "range_interior")
        self.assertEqual(roles["p5"], "range_end")

    def test_reversed_range_is_pending_ends_not_swapped(self):
        eng = self.engine(
            {"CR": range_citation("CR", "V1", "p5", "p2", PENDING)}
        )
        result = eng.expand_ref("V1", "rev1", {"citation_id": "CR"})
        self.assertEqual(result.status, PENDING)
        # 来源顺序保持 p5 在前、p2 在后，不交换。
        self.assertEqual([p.page_id for p in result.pages], ["p5", "p2"])
        self.assertTrue(result.detail["reversed"])

    def test_range_endpoint_inactive_is_pending(self):
        eng = self.engine(
            {"CR": range_citation("CR", "V1", "p3", "p5", PENDING)}
        )
        result = eng.expand_ref("V1", "rev1", {"citation_id": "CR"})
        self.assertEqual(result.status, PENDING)
        self.assertEqual(result.detail["reason"], "range_endpoint_inactive")

    def test_cross_volume_citation_is_flagged(self):
        eng = self.engine({"CX": pinned("CX", "V2", ["z"])})
        result = eng.expand_beat("V1", "rev1", 1, [{"citation_id": "CX"}])
        self.assertEqual(result.status, CROSS_VOLUME)
        self.assertEqual(
            result.ref_results[0].detail["citation_volume_id"], "V2"
        )

    def test_inline_label_resolves_unique_active_page(self):
        eng = self.engine({})
        result = eng.expand_beat(
            "V1", "rev1", 1, [{"section_id": "appendix", "label": "3"}]
        )
        self.assertEqual(result.status, OK)
        self.assertEqual([k[1] for k in result.page_keys], ["p4"])

    def test_inline_label_not_found_when_only_inactive_match(self):
        eng = self.engine({})
        result = eng.expand_beat(
            "V1", "rev1", 1, [{"section_id": "body", "label": "3"}]
        )
        self.assertEqual(result.status, RESOLUTION_NOT_FOUND)

    def test_inline_ambiguous_label_reports_all_matches(self):
        specs = [
            ("a", "appendix", "3", True),
            ("b", "appendix", "3", True),
        ]
        snap = make_snapshot(specs)
        layout_from_snapshot(snap, "d")
        eng = ExpansionEngine({"V1": snap}, {})
        result = eng.expand_beat(
            "V1", "d", 1, [{"section_id": "appendix", "label": "3"}]
        )
        self.assertEqual(result.status, RESOLUTION_AMBIGUOUS)
        self.assertEqual(
            sorted(result.ref_results[0].detail["matching_page_ids"]),
            ["a", "b"],
        )

    def test_beat_with_pages_on_two_openings_is_not_co_displayable(self):
        eng = self.engine({})
        result = eng.expand_beat(
            "V1", "rev1", 2, [{"page_id": "p1"}, {"page_id": "p5"}]
        )
        self.assertEqual(result.status, NOT_CO_DISPLAYABLE)
        self.assertIsNone(result.required_opening_no)

    def test_beat_with_two_pages_same_opening_is_ok(self):
        eng = self.engine({})
        result = eng.expand_beat(
            "V1", "rev1", 2, [{"page_id": "p1"}, {"page_id": "p2"}]
        )
        self.assertEqual(result.status, OK)
        self.assertEqual(result.required_opening_no, 1)

    def test_expansion_cache_shared_per_revision(self):
        eng = self.engine({"CP": pinned("CP", "V1", ["p1"])})
        ref = {"citation_id": "CP"}
        first = eng.expand_ref("V1", "rev1", ref)
        second = eng.expand_ref("V1", "rev1", dict(ref))
        self.assertIs(first, second)
        self.assertEqual(eng.cache_identity("V1", "rev1")["cached_refs"], 1)


class SolverBasicTests(unittest.TestCase):
    def setUp(self):
        # 题面字母例：p q r s 依次在用，默认台型，面1=pq 面2=rs。
        specs = [(x, "body", x, True) for x in "pqrs"]
        self.snap = make_snapshot(specs)
        layout_from_snapshot(self.snap, "default")
        self.eng = ExpansionEngine({"V1": self.snap}, {})

    def plan(self, beats, gaps, initial=1):
        return {
            "volume_id": "V1",
            "revision_id": "default",
            "beats": beats,
            "parts": [
                {"part_id": "a", "initial_opening": initial, "gaps": gaps}
            ],
        }

    def test_forward_one_face_reaches_r_with_distance_one(self):
        plan = self.plan(
            [
                {"beat": 1, "refs": [{"page_id": "p"}]},
                {"beat": 2, "refs": [{"page_id": "r"}]},
            ],
            [{"after_beat": 1, "forward": 1, "backward": 1}],
        )
        part = solve_rehearsal(self.eng, "V1", "default", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        self.assertEqual(part["total_distance"], 1)
        self.assertEqual(part["faces"], [1, 2])

    def test_blocked_gap_has_no_solution_and_reports_beat_and_reachable(self):
        plan = self.plan(
            [
                {"beat": 1, "refs": [{"page_id": "p"}]},
                {"beat": 2, "refs": [{"page_id": "r"}]},
            ],
            [{"after_beat": 1, "blocked": True}],
        )
        part = solve_rehearsal(self.eng, "V1", "default", plan)["parts"][0]
        self.assertFalse(part["feasible"])
        self.assertEqual(part["failure"]["beat"], 2)
        self.assertEqual(part["failure"]["reachable_openings"], [1])
        self.assertEqual(part["failure"]["required_openings"], [2])

    def test_revision_moving_r_before_p_allows_zero_movement_while_old_still_fails(self):
        # 新目录修订把 r 移到 p 前：面1 = r,p。
        layout_from_snapshot(
            self.snap, "new", active_page_ids=["r", "p", "q", "s"]
        )
        beats = [
            {"beat": 1, "refs": [{"page_id": "p"}]},
            {"beat": 2, "refs": [{"page_id": "r"}]},
        ]
        gaps = [{"after_beat": 1, "blocked": True}]
        old = self.plan(beats, gaps)
        old["revision_id"] = "default"
        new = self.plan(beats, gaps)
        new["revision_id"] = "new"
        old_part = solve_rehearsal(self.eng, "V1", "default", old)["parts"][0]
        new_part = solve_rehearsal(self.eng, "V1", "new", new)["parts"][0]
        self.assertFalse(old_part["feasible"])
        self.assertTrue(new_part["feasible"])
        self.assertEqual(new_part["total_distance"], 0)
        self.assertEqual(new_part["faces"], [1, 1])

    def test_rest_beat_has_no_requirement_but_still_respects_gap_limits(self):
        # 两台面；第1拍面1，第2拍空，第3拍面2。间隙1只许后翻（forward 0），
        # 空拍也无法前移，第2拍只能停在面1；间隙2同样不许前翻，第3拍不可达。
        plan = self.plan(
            [
                {"beat": 1, "refs": [{"page_id": "p"}]},
                {"beat": 2, "refs": []},
                {"beat": 3, "refs": [{"page_id": "r"}]},
            ],
            [
                {"after_beat": 1, "forward": 0, "backward": 1},
                {"after_beat": 2, "forward": 0, "backward": 1},
            ],
        )
        part = solve_rehearsal(self.eng, "V1", "default", plan)["parts"][0]
        self.assertFalse(part["feasible"])
        self.assertEqual(part["failure"]["beat"], 3)
        # 失败当时只能停在面1。
        self.assertEqual(part["failure"]["reachable_openings"], [1])

    def test_rest_beat_allows_turning_early_to_save_distance(self):
        # 空拍不要求内容，但允许在空拍间隙翻页：第2拍空拍时前移到面2，
        # 第3拍零移动即可，总距离 1。
        plan = self.plan(
            [
                {"beat": 1, "refs": [{"page_id": "p"}]},
                {"beat": 2, "refs": []},
                {"beat": 3, "refs": [{"page_id": "r"}]},
            ],
            [
                {"after_beat": 1, "forward": 1, "backward": 0},
                {"after_beat": 2, "forward": 1, "backward": 0},
            ],
        )
        part = solve_rehearsal(self.eng, "V1", "default", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        # 空拍提前翻或下一拍再翻，距离都为 1；题目允许任一合法排法。
        self.assertEqual(part["total_distance"], 1)
        self.assertEqual(part["moves"], [1, 0]) if part["faces"][1] == 2 \
            else self.assertEqual(part["moves"], [0, 1])
        self.assertEqual(part["faces"][0], 1)
        self.assertEqual(part["faces"][-1], 2)

    def test_minimal_distance_round_trip(self):
        snap = six_page_snapshot()  # 面1=p1p2 面2=p3p4 面3=p5p6
        eng = ExpansionEngine({"V1": snap}, {})
        plan = {
            "volume_id": "V1",
            "revision_id": "default",
            "beats": [
                {"beat": 1, "refs": [{"page_id": "p1"}]},
                {"beat": 2, "refs": [{"page_id": "p5"}]},
                {"beat": 3, "refs": [{"page_id": "p1"}]},
            ],
            "parts": [
                {
                    "part_id": "a",
                    "initial_opening": 1,
                    "gaps": [
                        {"after_beat": 1, "forward": 3, "backward": 3},
                        {"after_beat": 2, "forward": 3, "backward": 3},
                    ],
                }
            ],
        }
        part = solve_rehearsal(eng, "V1", "default", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        self.assertEqual(part["faces"], [1, 3, 1])
        self.assertEqual(part["total_distance"], 4)
        self.assertEqual(part["moves"], [2, -2])

    def test_solve_part_rejects_missing_gap(self):
        from citation_system.rehearsal.solver import BeatRequirement

        reqs = [
            BeatRequirement(1, frozenset({1}), OK),
            BeatRequirement(2, frozenset({2}), OK),
        ]
        with self.assertRaises(ValueError):
            solve_part(reqs, 1, [], 2)


class SentenceEditionTests(unittest.TestCase):
    def setUp(self):
        # 六页三台面：面1=p1p2，面2=p3p4，面3=p5p6。
        self.snap = six_page_snapshot()
        self.citations = {
            "C1": pinned("C1", "V1", ["p1"]),
            "CA2": pinned("CA2", "V1", ["p1"]),
            "CA3": pinned("CA3", "V1", ["p5"]),
            "CB2": pinned("CB2", "V1", ["p3"]),
            "CB3": pinned("CB3", "V1", ["p3"]),
        }
        self.eng = ExpansionEngine({"V1": self.snap}, self.citations)
        self.beats = [
            {"beat": 1, "refs": [{"citation_id": "C1"}]},
            {
                "beat": 2,
                "sentence_id": "S1",
                "alternatives": {
                    "A": {"refs": [{"citation_id": "CA2"}]},
                    "B": {"refs": [{"citation_id": "CB2"}]},
                },
            },
            {
                "beat": 3,
                "sentence_id": "S1",
                "alternatives": {
                    "A": {"refs": [{"citation_id": "CA3"}]},
                    "B": {"refs": [{"citation_id": "CB3"}]},
                },
            },
        ]

    def plan(self, gap1, gap2=None):
        gap2 = gap2 or {"after_beat": 2, "forward": 1, "backward": 1}
        return {
            "rehearsal_id": "R",
            "volume_id": "V1",
            "revision_id": "default",
            "beats": self.beats,
            "parts": [
                {
                    "part_id": "vn",
                    "initial_opening": 1,
                    "gaps": [gap1, gap2],
                }
            ],
        }

    def test_only_edition_b_plays_with_total_distance_one(self):
        plan = self.plan({"after_beat": 1, "forward": 1, "backward": 1})
        part = solve_rehearsal(self.eng, "V1", "default", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        self.assertEqual(part["sentence_editions"], [{"sentence_id": "S1", "edition_id": "B"}])
        self.assertEqual(part["faces"], [1, 2, 2])
        self.assertEqual(part["total_distance"], 1)

    def test_blocked_first_gap_kills_both_editions_no_mixing(self):
        plan = self.plan({"after_beat": 1, "blocked": True})
        part = solve_rehearsal(self.eng, "V1", "default", plan)["parts"][0]
        self.assertFalse(part["feasible"])
        self.assertEqual(part["failure"]["beat"], 2)
        # 没有任何句子版本被选用——不能拼甲首拍与乙末拍。
        self.assertEqual(part["sentence_editions"], [])

    def test_structurally_dead_editions_report_when_all_choices_fail(self):
        citations = dict(self.citations)
        citations["CA3"] = pinned("CA3", "V1", ["p5"], PENDING)
        citations["CB3"] = pinned("CB3", "V1", ["p3"], PENDING)
        eng = ExpansionEngine({"V1": self.snap}, citations)
        plan = self.plan({"after_beat": 1, "forward": 1, "backward": 1})
        part = solve_rehearsal(eng, "V1", "default", plan)["parts"][0]
        self.assertFalse(part["feasible"])
        self.assertEqual(part["failure"]["beat"], 3)
        self.assertEqual(part["failure"]["reason"], "all_edition_choices_failed")
        failed = {
            f["edition_id"]: f["reason"]
            for f in part["failure"]["detail"]["edition_failures"]
        }
        self.assertEqual(failed, {"A": PENDING, "B": PENDING})

    def test_one_dead_edition_still_allows_the_other_whole_book(self):
        citations = dict(self.citations)
        citations["CB3"] = pinned("CB3", "V1", ["p3"], PENDING)  # 乙待核
        eng = ExpansionEngine({"V1": self.snap}, citations)
        plan = self.plan(
            {"after_beat": 1, "forward": 2, "backward": 2},
            {"after_beat": 2, "forward": 2, "backward": 2},
        )
        part = solve_rehearsal(eng, "V1", "default", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        self.assertEqual(
            part["sentence_editions"], [{"sentence_id": "S1", "edition_id": "A"}]
        )
        self.assertEqual(part["faces"], [1, 1, 3])

    def test_changing_stand_type_makes_edition_playable(self):
        # 台型改为卷首空位：p1 独占面2，p3 在面3。初始面改为 2（p1 所在）。
        snap = six_page_snapshot(stand=STAND_LEADING_BLANK)
        eng = ExpansionEngine({"V1": snap}, self.citations)
        plan = self.plan({"after_beat": 1, "forward": 1, "backward": 1})
        plan["parts"][0]["initial_opening"] = 2
        part = solve_rehearsal(eng, "V1", "default", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        # 乙第2拍 p3 在面3，距面2正好一面，可演。
        self.assertEqual(
            part["sentence_editions"], [{"sentence_id": "S1", "edition_id": "B"}]
        )

    def test_changing_catalog_revision_makes_edition_playable(self):
        # 新修订把 p3 移到 p1 同面：面1 = p3,p1，于是乙第2拍零移动可演。
        snap = six_page_snapshot()
        layout_from_snapshot(
            snap, "revised",
            active_page_ids=["p3", "p1", "p2", "p4", "p5", "p6"],
        )
        eng = ExpansionEngine({"V1": snap}, self.citations)
        plan = self.plan({"after_beat": 1, "blocked": True})
        plan["revision_id"] = "revised"
        part = solve_rehearsal(eng, "V1", "revised", plan)["parts"][0]
        self.assertTrue(part["feasible"])
        self.assertEqual(
            part["sentence_editions"], [{"sentence_id": "S1", "edition_id": "B"}]
        )
        self.assertEqual(part["total_distance"], 0)


class SharedExpansionAndSealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        specs = [(f"p{i}", "body", str(i), True) for i in range(1, 7)]
        self.snap = make_snapshot(specs)
        layout_from_snapshot(self.snap, "default")
        self.citations = {"CP": pinned("CP", "V1", ["p1"])}
        self.plan = {
            "rehearsal_id": "OFF",
            "volume_id": "V1",
            "revision_id": "default",
            "beats": [
                {"beat": 1, "refs": [{"citation_id": "CP"}]},
                {"beat": 2, "refs": [{"page_id": "p5"}]},
            ],
            "parts": [
                {
                    "part_id": "vn1",
                    "initial_opening": 1,
                    "gaps": [{"after_beat": 1, "forward": 2, "backward": 2}],
                },
                {
                    "part_id": "vn2",  # 不同声部、不同休止节奏，共享展开
                    "initial_opening": 1,
                    "gaps": [{"after_beat": 1, "forward": 2, "backward": 2}],
                },
            ],
        }

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_multiple_parts_share_one_expansion_cache(self):
        eng = ExpansionEngine({"V1": self.snap}, self.citations)
        solution = solve_rehearsal(eng, "V1", "default", self.plan)
        self.assertEqual(len(solution["parts"]), 2)
        # 两个声部引用同一批展开，缓存只算一次。
        self.assertEqual(solution["expansion_cache"]["cached_refs"], 2)
        self.assertTrue(all(p["feasible"] for p in solution["parts"]))

    def test_seal_and_offline_replay_and_tamper_detection(self):
        eng = ExpansionEngine({"V1": self.snap}, self.citations)
        solution = solve_rehearsal(eng, "V1", "default", self.plan)
        bundle_dir = self.tmp / "bundle"
        info = seal_rehearsal(
            bundle_dir, {"V1": self.snap}, self.citations, self.plan,
            solution=solution,
        )
        verification = verify_bundle(bundle_dir)
        self.assertTrue(verification.hash_ok)
        self.assertTrue(verification.replay_ok)
        self.assertTrue(verification.ok)
        # 断网重放出相同逐拍面号。
        replayed = verification.details["replayed"]
        self.assertEqual(
            [p["faces"] for p in replayed["parts"]],
            [p["faces"] for p in solution["parts"]],
        )
        # 篡改封存结果即复核失败（哈希与重放同时不通过）。
        bundle_path = bundle_dir / info["bundle_file"].split("/")[-1]
        doc = json.loads(bundle_path.read_text(encoding="utf-8"))
        doc["sealed_solution"]["parts"][0]["faces"] = [9, 9]
        bundle_path.write_text(json.dumps(doc), encoding="utf-8")
        bad = verify_bundle(bundle_dir)
        self.assertFalse(bad.ok)
        self.assertFalse(bad.hash_ok)

    def test_bundle_rebuilds_from_document_without_sqlite(self):
        eng = ExpansionEngine({"V1": self.snap}, self.citations)
        solution = solve_rehearsal(eng, "V1", "default", self.plan)
        bundle_dir = self.tmp / "bundle2"
        seal_rehearsal(
            bundle_dir, {"V1": self.snap}, self.citations, self.plan,
            solution=solution,
        )
        document = json.loads(
            (bundle_dir / "bundle.json").read_text(encoding="utf-8")
        )
        snapshots = snapshot_from_document({"volumes": document["volumes"]})
        self.assertIn("V1", snapshots)
        rebuilt = snapshots["V1"]
        self.assertEqual(
            [p.page_id for p in rebuilt.layouts["default"][0].pages],
            ["p1", "p2"],
        )


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog.create(str(self.tmp / "cat.db"))
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {
                        "volume_id": "V1",
                        "pages": [
                            {"page_id": "p1", "section_id": "body", "label": "1", "active": True},
                            {"page_id": "p2", "section_id": "body", "label": "2", "active": True},
                            {"page_id": "p3", "section_id": "body", "label": "3", "active": True},
                            {"page_id": "p4", "section_id": "body", "label": "4", "active": True},
                        ],
                    }
                ],
            }
        )
        self.repo = CitationRepository(self.cat)
        self.repo.create_pinned("CP", "固定", "V1", ["p1"])
        self.repo.create_range("CR", "范围", "V1", "p2", "p4")

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def test_snapshot_and_registry_round_trip_through_real_catalog(self):
        snapshots = snapshot_from_catalog(self.cat)
        layout_from_snapshot(snapshots["V1"], "default")
        registry = seal_mod.citation_registry_from_repo(self.cat, self.repo)
        self.assertEqual(registry["CP"]["kind"], "PINNED")
        self.assertEqual(registry["CR"]["kind"], "RANGE")
        eng = ExpansionEngine(snapshots, registry)
        result = eng.expand_ref("V1", "default", {"citation_id": "CR"})
        self.assertEqual([p.page_id for p in result.pages], ["p2", "p3", "p4"])

    def test_deactivation_makes_pinned_pending_in_rehearsal(self):
        self.cat.apply_batch(
            {"operation": "deactivate", "volume_id": "V1", "page_id": "p1"}
        )
        snapshots = snapshot_from_catalog(self.cat)
        layout_from_snapshot(snapshots["V1"], "default")
        registry = seal_mod.citation_registry_from_repo(self.cat, self.repo)
        self.assertEqual(registry["CP"]["status"], PENDING)
        eng = ExpansionEngine(snapshots, registry)
        result = eng.expand_beat(
            "V1", "default", 1, [{"citation_id": "CP"}]
        )
        self.assertEqual(result.status, PENDING)


class CliRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "cat.db"
        self.cat = Catalog.create(str(self.db_path))
        self.cat.load_pages_document(
            {
                "format": "volume-pages/1",
                "revision": 1,
                "volumes": [
                    {
                        "volume_id": "V1",
                        "pages": [
                            {"page_id": f"p{i}", "section_id": "body",
                             "label": str(i), "active": True}
                            for i in range(1, 7)
                        ],
                    }
                ],
            }
        )
        self.repo = CitationRepository(self.cat)
        for cid, pid in [("C1", "p1"), ("CA2", "p1"), ("CA3", "p5"),
                         ("CB2", "p3"), ("CB3", "p3")]:
            self.repo.create_pinned(cid, cid, "V1", [pid])
        self.cat.close()
        self.plan_path = self.tmp / "plan.json"
        self.plan_path.write_text(
            json.dumps(
                {
                    "format": "rehearsal-plan/1",
                    "rehearsal_id": "R",
                    "volume_id": "V1",
                    "revision_id": "default",
                    "layouts": [
                        {"revision_id": "default", "stand_type": "paired"}
                    ],
                    "beats": [
                        {"beat": 1, "refs": [{"citation_id": "C1"}]},
                        {
                            "beat": 2, "sentence_id": "S1",
                            "alternatives": {
                                "A": {"refs": [{"citation_id": "CA2"}]},
                                "B": {"refs": [{"citation_id": "CB2"}]},
                            },
                        },
                        {
                            "beat": 3, "sentence_id": "S1",
                            "alternatives": {
                                "A": {"refs": [{"citation_id": "CA3"}]},
                                "B": {"refs": [{"citation_id": "CB3"}]},
                            },
                        },
                    ],
                    "parts": [
                        {
                            "part_id": "vn", "initial_opening": 1,
                            "gaps": [
                                {"after_beat": 1, "forward": 1, "backward": 1},
                                {"after_beat": 2, "forward": 1, "backward": 1},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "citation_system", *args],
            capture_output=True, text=True, cwd=ROOT,
        )

    def test_rehearse_seal_offline_replay_verify_cli(self):
        bundle_dir = self.tmp / "bundle"
        proc = self._run(
            "rehearse", "--db", str(self.db_path),
            "--plan", str(self.plan_path), "--seal", str(bundle_dir),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sealed = json.loads(proc.stdout)
        part = sealed["solution"]["parts"][0]
        self.assertTrue(part["feasible"])
        self.assertEqual(
            part["sentence_editions"], [{"sentence_id": "S1", "edition_id": "B"}]
        )

        # 断网复核（只读 bundle，不打开数据库）。
        verify = self._run("verify-bundle", "--bundle", str(bundle_dir))
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["ok"])

        # 离线重放。
        offline = self._run(
            "rehearse-offline", "--bundle", str(bundle_dir)
        )
        self.assertEqual(offline.returncode, 0, offline.stderr)
        replayed = json.loads(offline.stdout)["solution"]["parts"][0]
        self.assertEqual(replayed["faces"], part["faces"])


class SentenceStructureTests(unittest.TestCase):
    def test_interrupted_sentence_is_rejected(self):
        from citation_system.rehearsal.solver import _build_segments
        with self.assertRaises(ValueError):
            _build_segments([
                {"beat": 1, "sentence_id": "S1",
                 "alternatives": {"A": {}, "B": {}}},
                {"beat": 2, "sentence_id": "S2",
                 "alternatives": {"A": {}, "B": {}}},
                {"beat": 3, "sentence_id": "S1",
                 "alternatives": {"A": {}, "B": {}}},
            ])

    def test_sentence_with_changing_edition_set_is_rejected(self):
        from citation_system.rehearsal.solver import _build_segments
        with self.assertRaises(ValueError):
            _build_segments([
                {"beat": 1, "sentence_id": "S1",
                 "alternatives": {"A": {}, "B": {}}},
                {"beat": 2, "sentence_id": "S1",
                 "alternatives": {"A": {}, "C": {}}},
            ])

    def test_distinct_adjacent_sentences_are_separate_segments(self):
        from citation_system.rehearsal.solver import _build_segments
        segs = _build_segments([
            {"beat": 1, "sentence_id": "S1",
             "alternatives": {"A": {}, "B": {}}},
            {"beat": 2, "sentence_id": "S2",
             "alternatives": {"A": {}, "B": {}}},
        ])
        self.assertEqual(
            [(s.sentence_id, s.start_beat, s.end_beat) for s in segs],
            [("S1", 1, 1), ("S2", 2, 2)],
        )


class RandomInvariantTests(unittest.TestCase):
    """用独立的暴力枚举交叉验证 DP 的最优性与约束满足。"""

    def setUp(self):
        import random

        self.rng = random.Random(20261121)

    def _brute_force_min(self, required_per_beat, gaps, initial, opening_count):
        # 枚举所有逐拍面序列，返回最小距离（不可行返回 None）。小规模使用。
        best = [None]

        def rec(beat, face, cost, faces):
            # faces 已含第 1..beat 拍；现在决定第 beat+1 拍的面（下标 beat）。
            if beat == len(required_per_beat):
                if best[0] is None or cost < best[0][0]:
                    best[0] = (cost, list(faces))
                return
            req = required_per_beat[beat]
            # 从第 beat 拍走到第 beat+1 拍，经过的是 gaps[beat-1]。
            gap = gaps[beat - 1]
            for nxt in range(1, opening_count + 1):
                delta = nxt - face
                if not gap.allows(delta):
                    continue
                if req is not None and nxt not in req:
                    continue
                rec(beat + 1, nxt, cost + abs(delta), faces + [nxt])

        # 第 1 拍固定为起始面，先检查它是否满足第 1 拍要求。
        first_req = required_per_beat[0]
        if first_req is not None and initial not in first_req:
            return None
        rec(1, initial, 0, [initial])
        return None if best[0] is None else best[0]

    def test_dp_matches_brute_force_across_random_cases(self):
        from citation_system.rehearsal.solver import BeatRequirement

        for _ in range(60):
            opening_count = self.rng.randint(2, 4)
            beat_count = self.rng.randint(2, 6)
            initial = self.rng.randint(1, opening_count)
            gaps = []
            for _ in range(beat_count - 1):
                if self.rng.random() < 0.25:
                    gaps.append(Gap(0, 0, 0))  # 禁翻
                else:
                    gaps.append(Gap(
                        0,
                        self.rng.randint(0, opening_count),
                        self.rng.randint(0, opening_count),
                    ))
            reqs = []
            required_per_beat = []
            for _ in range(beat_count):
                if self.rng.random() < 0.3:
                    reqs.append(BeatRequirement(0, None, OK))  # 空拍
                    required_per_beat.append(None)
                else:
                    allowed = frozenset(
                        self.rng.sample(
                            range(1, opening_count + 1),
                            self.rng.randint(1, opening_count),
                        )
                    )
                    reqs.append(BeatRequirement(0, allowed, OK))
                    required_per_beat.append(set(allowed))
            result = solve_part(reqs, initial, gaps, opening_count)
            expected = self._brute_force_min(
                required_per_beat, gaps, initial, opening_count
            )
            if expected is None:
                self.assertFalse(result.feasible)
            else:
                self.assertTrue(result.feasible)
                self.assertEqual(result.total_distance, expected[0])
                self.assertEqual(result.faces, tuple(expected[1]))
                # 逐间隙校验速率约束。
                for i, gap in enumerate(gaps):
                    self.assertTrue(
                        gap.allows(result.faces[i + 1] - result.faces[i])
                    )


if __name__ == "__main__":
    unittest.main()
