"""排练排页的命令行入口（在线建包 / 离线重放 / 封存复核）。

这些函数被 :mod:`citation_system.cli` 挂成子命令，也可以单独调用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .expansion import ExpansionEngine
from .model import LayoutConfig, build_layout, snapshot_from_catalog
from .seal import (
    engine_from_bundle,
    seal_rehearsal,
    verify_bundle,
    citation_registry_from_repo,
)
from .solver import solve_rehearsal

PLAN_FORMAT = "rehearsal-plan/1"


def _read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _apply_plan_layouts(
    snapshots: dict[str, Any], plan: dict[str, Any]
) -> None:
    default_volume = plan["volume_id"]
    for layout in plan.get("layouts", []):
        vid = layout.get("volume_id", default_volume)
        snapshot = snapshots[vid]
        if "active_order" in layout:
            active_order = tuple(
                (item["volume_id"], item["page_id"])
                for item in layout["active_order"]
            )
            inactive = frozenset(
                (item["volume_id"], item["page_id"])
                for item in layout.get("inactive_page_ids", [])
            )
        else:
            page_ids = layout.get("active_page_ids")
            if page_ids is None:
                page_ids = [
                    p.page_id for p in snapshot.physical_order if p.active
                ]
            active_order = tuple((vid, pid) for pid in page_ids)
            active_keys = set(active_order)
            inactive = frozenset(
                p.as_key()
                for p in snapshot.physical_order
                if p.as_key() not in active_keys
            )
        config = LayoutConfig(
            volume_id=vid,
            revision_id=layout["revision_id"],
            stand_type=layout.get("stand_type", "paired"),
            active_order=active_order,
            inactive_page_ids=inactive,
            catalog_revision=snapshot.catalog_revision,
        )
        snapshot.layouts[config.revision_id] = build_layout(
            config, snapshot.pages
        )


def cmd_rehearse(args: Any) -> int:
    """在线：从 Catalog 库 + 排练计划求解，并可封存离线包。"""

    from citation_system.catalog import Catalog
    from citation_system.repository import CitationRepository

    plan = _read_json(args.plan)
    if plan.get("format") not in (PLAN_FORMAT, None):
        raise SystemExit(f"unsupported plan format {plan.get('format')!r}")

    catalog = Catalog.open(args.db)
    try:
        snapshots = snapshot_from_catalog(catalog)
        _apply_plan_layouts(snapshots, plan)
        repo = CitationRepository(catalog)
        citations = citation_registry_from_repo(catalog, repo)
    finally:
        catalog.close()

    engine = ExpansionEngine(snapshots, citations)
    solution = solve_rehearsal(
        engine, plan["volume_id"], plan["revision_id"], plan
    )
    out: dict[str, Any] = {"solution": solution}
    if args.seal:
        sealed = seal_rehearsal(
            args.seal, snapshots, citations, plan, solution=solution
        )
        out["sealed"] = sealed
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_rehearse_offline(args: Any) -> int:
    """离线：在封存包上重放；可另给计划试不同休止安排（不修改封存包）。"""

    engine, document = engine_from_bundle(args.bundle)
    if args.plan:
        plan = _read_json(args.plan)
        # 离线计划只能引用包里已有的卷与修订；若自带布局，必须用稳定身份。
        _apply_plan_layouts(engine.snapshots, plan)
        volume_id = plan["volume_id"]
        revision_id = plan["revision_id"]
        rehearsal = plan
    else:
        rehearsal = document["rehearsal"]
        volume_id = rehearsal["volume_id"]
        revision_id = rehearsal["revision_id"]
    solution = solve_rehearsal(
        engine, volume_id, revision_id, rehearsal
    )
    print(json.dumps({"solution": solution}, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_bundle(args: Any) -> int:
    verification = verify_bundle(args.bundle)
    print(json.dumps(verification.as_dict(), ensure_ascii=False, indent=2))
    return 0 if verification.ok else 2


def add_rehearsal_subparsers(sub: Any) -> None:
    p = sub.add_parser(
        "rehearse",
        help="solve a rehearsal plan from the catalog and optionally seal it",
    )
    p.add_argument("--db", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument(
        "--seal",
        help="write a self-contained offline rehearsal bundle to this directory",
    )
    p.set_defaults(func=cmd_rehearse)

    p = sub.add_parser(
        "rehearse-offline",
        help="replay (or re-plan) a sealed rehearsal bundle without SQLite/network",
    )
    p.add_argument("--bundle", required=True)
    p.add_argument("--plan", help="alternative rehearsal plan over bundle data")
    p.set_defaults(func=cmd_rehearse_offline)

    p = sub.add_parser(
        "verify-bundle", help="check a sealed bundle hash and replay its solution"
    )
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_verify_bundle)
