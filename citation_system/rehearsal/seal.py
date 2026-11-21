"""排练封存与断网复核。

一场排练封存后是一个自包含目录（``rehearsal-bundle/1``）：

* ``bundle.json``    -- 快照页（含停用页与物理 seq）、目录修订与台型、
  引用登记（固定集合/范围端页/待核状态）、整场排练输入（声部、间隙、句子
  替代谱本）与求解结果。
* ``manifest.sha256`` -- 数据文件的 SHA-256 清单。

复核端不需要 SQLite、不需要网络：只凭包内的稳定页身份重建
:class:`~citation_system.rehearsal.model.VolumeSnapshot` 与展开引擎，重跑
同一个求解器，再把结果与封存记录逐字段比对，并校验哈希。这样"一场已封存
的排练须可在断网台上复核"，且旧版目录（例如仍无解的修订）原样保留。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .expansion import ExpansionEngine
from .model import (
    VolumeSnapshot,
    snapshot_from_document,
)
from .solver import solve_rehearsal

BUNDLE_FORMAT = "rehearsal-bundle/1"
BUNDLE_FILE = "bundle.json"
MANIFEST_FILE = "manifest.sha256"


# ----------------------------------------------------------------------
# 引用登记：把库里的 PINNED/RANGE 引用转成离线可重建的登记形状
# ----------------------------------------------------------------------
def citation_registry_from_repo(catalog: Any, repo: Any) -> dict[str, dict[str, Any]]:
    """从在线 Catalog/Repository 构造引用登记表。"""

    registry: dict[str, dict[str, Any]] = {}
    for citation in repo.all():
        cid = citation.citation_id
        if citation.kind == "PINNED":
            members = repo.pinned_members(cid)
            inactive = [p for p in members if not p.active]
            registry[cid] = {
                "citation_id": cid,
                "text": citation.text,
                "kind": "PINNED",
                "volume_id": citation.volume_id,
                "members": [
                    {"volume_id": p.volume_id, "page_id": p.page_id}
                    for p in members
                ],
                "status": "pending" if inactive else "usable",
            }
        else:
            start, end = repo.range_endpoints(cid)
            pending = (
                not start.active
                or not end.active
                or start.seq > end.seq
            )
            registry[cid] = {
                "citation_id": cid,
                "text": citation.text,
                "kind": "RANGE",
                "volume_id": citation.volume_id,
                "start_page_id": start.page_id,
                "end_page_id": end.page_id,
                "status": "pending" if pending else "usable",
            }
    return registry


def layouts_for_volume(
    snapshot: VolumeSnapshot,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for revision_id, openings in snapshot.layouts.items():
        # 由对开表反推修订配置，避免再要求外部同时保存 LayoutConfig。
        active_order: list[dict[str, str]] = []
        for opening in openings:
            for page in opening.pages:
                active_order.append(
                    {"volume_id": page.volume_id, "page_id": page.page_id}
                )
        active_keys = {(d["volume_id"], d["page_id"]) for d in active_order}
        inactive = [
            p.as_dict()
            for p in snapshot.physical_order
            if p.as_key() not in active_keys
        ]
        stand_type = (
            "leading_blank"
            if openings and openings[0].leading_blank
            else "paired"
        )
        out.append({
            "revision_id": revision_id,
            "stand_type": stand_type,
            "catalog_revision": snapshot.catalog_revision,
            "active_order": active_order,
            "inactive_page_ids": [
                {"volume_id": d["volume_id"], "page_id": d["page_id"]}
                for d in inactive
            ],
        })
    return out


# ----------------------------------------------------------------------
# 封存
# ----------------------------------------------------------------------
def build_bundle_document(
    snapshots: dict[str, VolumeSnapshot],
    citations: dict[str, dict[str, Any]],
    rehearsal: dict[str, Any],
    *,
    solution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "format": BUNDLE_FORMAT,
        "rehearsal_id": rehearsal.get("rehearsal_id"),
        "sealed": True,
        "volumes": [
            {
                "volume_id": vid,
                "catalog_revision": snap.catalog_revision,
                "pages": [p.as_dict() for p in snap.physical_order],
                "layouts": layouts_for_volume(snap),
            }
            for vid, snap in sorted(snapshots.items())
        ],
        "citations": [citations[k] for k in sorted(citations)],
        "rehearsal": rehearsal,
        "sealed_solution": solution,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal_rehearsal(
    path: str | Path,
    snapshots: dict[str, VolumeSnapshot],
    citations: dict[str, dict[str, Any]],
    rehearsal: dict[str, Any],
    *,
    solution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一场排练封存在目录 ``path`` 并写哈希清单。"""

    out_dir = Path(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    document = build_bundle_document(
        snapshots, citations, rehearsal, solution=solution
    )
    bundle_path = out_dir / BUNDLE_FILE
    tmp = bundle_path.with_name(BUNDLE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, bundle_path)

    digest = _sha256_file(bundle_path)
    manifest = f"{digest}  {BUNDLE_FILE}\n"
    (out_dir / MANIFEST_FILE).write_text(manifest, encoding="utf-8")
    return {
        "bundle_dir": str(out_dir),
        "bundle_file": str(bundle_path),
        "sha256": digest,
        "format": BUNDLE_FORMAT,
    }


# ----------------------------------------------------------------------
# 断网复核
# ----------------------------------------------------------------------
@dataclass
class Verification:
    ok: bool
    hash_ok: bool
    replay_ok: bool
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "hash_ok": self.hash_ok,
            "replay_ok": self.replay_ok,
            "details": self.details,
        }


def load_bundle(path: str | Path) -> dict[str, Any]:
    bundle_path = Path(path)
    if bundle_path.is_dir():
        bundle_path = bundle_path / BUNDLE_FILE
    with open(bundle_path, encoding="utf-8") as fh:
        return json.load(fh)


def verify_bundle(path: str | Path) -> Verification:
    """断网复核：哈希校验 + 重跑求解器并与封存结果比对。"""

    bundle_dir = Path(path)
    bundle_path = bundle_dir / BUNDLE_FILE if bundle_dir.is_dir() else bundle_dir
    document = load_bundle(bundle_path)
    if document.get("format") != BUNDLE_FORMAT:
        return Verification(False, False, False, {"reason": "bad_format"})

    # 1) 哈希清单。
    manifest_path = bundle_path.parent / MANIFEST_FILE
    hash_ok = False
    actual = _sha256_file(bundle_path)
    if manifest_path.exists():
        expected_line = manifest_path.read_text(encoding="utf-8").strip()
        expected = expected_line.split()[0] if expected_line else ""
        hash_ok = expected == actual

    # 2) 纯内存重建，不触碰 SQLite。
    snapshots = snapshot_from_document({"volumes": document["volumes"]})
    citations = {c["citation_id"]: c for c in document["citations"]}
    engine = ExpansionEngine(snapshots, citations)
    rehearsal = document["rehearsal"]
    replayed = solve_rehearsal(
        engine,
        rehearsal["volume_id"],
        rehearsal["revision_id"],
        rehearsal,
    )
    sealed = document.get("sealed_solution")
    replay_ok = sealed is not None and _solutions_equal(sealed, replayed)

    details = {
        "rehearsal_id": document.get("rehearsal_id"),
        "actual_sha256": actual,
        "replayed": replayed,
        "sealed_matches": replay_ok,
    }
    return Verification(hash_ok and replay_ok, hash_ok, replay_ok, details)


def _solutions_equal(sealed: dict[str, Any], replayed: dict[str, Any]) -> bool:
    """比较会影响演奏结论的字段；缓存统计等易变字段不参与。"""

    keys = ("rehearsal_id", "volume_id", "revision_id", "beat_count",
            "opening_count", "parts")
    for key in keys:
        if sealed.get(key) != replayed.get(key):
            return False
    return True


def engine_from_bundle(path: str | Path) -> tuple[ExpansionEngine, dict[str, Any]]:
    """供离线 CLI 直接在封存包上重新求解（也允许改输入后试排）。"""

    document = load_bundle(path)
    snapshots = snapshot_from_document({"volumes": document["volumes"]})
    citations = {c["citation_id"]: c for c in document["citations"]}
    return ExpansionEngine(snapshots, citations), document
