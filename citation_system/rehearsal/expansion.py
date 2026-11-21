"""把一拍指定的引用展成该版实际须看见的页、对开和来源历。

引擎只吃内存快照（:class:`~citation_system.rehearsal.model.VolumeSnapshot`）
与引用登记表，因此同一份输入在断网台上给出逐字节一致的展开。

状态码（每个被展开的引用一个）：

``ok``
    引用可用，且落在排练卷内，其全部实际页都已定位到对开。
``pending``
    引用本身待处理：PINNED 含停用成员，或 RANGE 端点停用/先后反转，
    或引用登记为待核（``status: pending``）。
``cross_volume``
    引用属于另一卷，读谱台本场固定卷无法显示。
``resolution_not_found`` / ``resolution_ambiguous``
    内联的 ``(section, label)`` 在本卷在用页里解析不到或解析出多个
    （同号附页）——只凭印刷页码会排错，这里直接判失败而不猜测。

一拍可以列多份引用；它们必须同显在**同一个对开**上，否则整拍归为
``not_co_displayable``，并附各页所在的对开号供指挥核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import PageRecord, VolumeSnapshot

OK = "ok"
PENDING = "pending"
CROSS_VOLUME = "cross_volume"
NOT_CO_DISPLAYABLE = "not_co_displayable"
RESOLUTION_NOT_FOUND = "resolution_not_found"
RESOLUTION_AMBIGUOUS = "resolution_ambiguous"

PINNED = "PINNED"
RANGE = "RANGE"


@dataclass(frozen=True)
class ProvenanceEntry:
    """一页"从哪来"的来源历条目。"""

    volume_id: str
    page_id: str
    label: str
    section_id: str
    role: str  # pinned_member | range_start | range_end | range_interior | inline
    citation_id: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {
            "volume_id": self.volume_id,
            "page_id": self.page_id,
            "label": self.label,
            "section_id": self.section_id,
            "role": self.role,
            "citation_id": self.citation_id,
        }
        if self.note is not None:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class RefExpansion:
    """单个引用（或内联页引用）的展开结果。"""

    ref: dict[str, Any]
    status: str
    kind: str
    citation_id: str | None
    pages: tuple[PageRecord, ...]
    opening_nos: tuple[int, ...]
    provenance: tuple[ProvenanceEntry, ...]
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "kind": self.kind,
            "citation_id": self.citation_id,
            "pages": [p.as_dict() for p in self.pages],
            "opening_nos": list(self.opening_nos),
            "provenance": [p.as_dict() for p in self.provenance],
            "detail": self.detail,
            "ref": self.ref,
        }


@dataclass(frozen=True)
class BeatExpansion:
    """一拍全部引用合并后的可显示性结论。"""

    beat: int
    status: str
    required_opening_no: int | None
    ref_results: tuple[RefExpansion, ...]
    page_keys: frozenset[tuple[str, str]]
    provenance: tuple[ProvenanceEntry, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "beat": self.beat,
            "status": self.status,
            "required_opening_no": self.required_opening_no,
            "page_keys": [
                {"volume_id": v, "page_id": p} for v, p in sorted(self.page_keys)
            ],
            "provenance": [p.as_dict() for p in self.provenance],
            "references": [r.as_dict() for r in self.ref_results],
        }


class ExpansionEngine:
    """按（卷、目录修订）展开引用；同版结果缓存，多声部/多排练共享。"""

    def __init__(
        self,
        snapshots: dict[str, VolumeSnapshot],
        citations: dict[str, dict[str, Any]],
    ) -> None:
        self.snapshots = snapshots
        self.citations = citations
        # 缓存键含目录修订与台型，故目录一改、台型一改都会自然失效重算。
        self._cache: dict[tuple[str, str, str], RefExpansion] = {}

    # ------------------------------------------------------------------
    def expand_ref(
        self, volume_id: str, revision_id: str, ref: dict[str, Any]
    ) -> RefExpansion:
        key = (volume_id, revision_id, _stable_ref_key(ref))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if "citation_id" in ref:
            result = self._expand_citation(volume_id, revision_id, ref)
        else:
            result = self._expand_inline(volume_id, revision_id, ref)
        self._cache[key] = result
        return result

    def expand_beat(
        self, volume_id: str, revision_id: str, beat: int, refs: list[dict[str, Any]]
    ) -> BeatExpansion:
        results = tuple(self.expand_ref(volume_id, revision_id, r) for r in refs)
        provenance = tuple(p for r in results for p in r.provenance)
        page_keys = frozenset(p.as_key() for r in results for p in r.pages)

        status = OK
        required: int | None = None
        for result in results:
            if result.status != OK:
                status = result.status
                break
        else:
            opening_sets = {r.opening_nos for r in results}
            all_nos = {no for r in results for no in r.opening_nos}
            if len(all_nos) != 1 or any(len(s) != 1 for s in opening_sets):
                status = NOT_CO_DISPLAYABLE
            else:
                required = next(iter(all_nos))

        return BeatExpansion(
            beat=beat,
            status=status,
            required_opening_no=required,
            ref_results=results,
            page_keys=page_keys,
            provenance=provenance,
        )

    def cache_size(self) -> int:
        return len(self._cache)

    def cache_identity(self, volume_id: str, revision_id: str) -> dict[str, Any]:
        """供离线包记录"按版共享了什么"的摘要。"""

        snapshot = self.snapshots[volume_id]
        layout = snapshot.layouts[revision_id]
        return {
            "volume_id": volume_id,
            "revision_id": revision_id,
            "catalog_revision": snapshot.catalog_revision,
            "opening_count": len(layout),
            "cached_refs": sum(1 for (v, r, _k) in self._cache if (v, r) == (volume_id, revision_id)),
        }

    # ------------------------------------------------------------------
    def _snapshot(self, volume_id: str) -> VolumeSnapshot:
        try:
            return self.snapshots[volume_id]
        except KeyError:
            raise ValueError(f"volume {volume_id!r} has no snapshot") from None

    def _expand_citation(
        self, volume_id: str, revision_id: str, ref: dict[str, Any]
    ) -> RefExpansion:
        cid = ref["citation_id"]
        citation = self.citations.get(cid)
        if citation is None:
            return RefExpansion(
                ref=ref,
                status=RESOLUTION_NOT_FOUND,
                kind="UNKNOWN",
                citation_id=cid,
                pages=(),
                opening_nos=(),
                provenance=(),
                detail={"reason": "unknown_citation_id"},
            )
        # 登记为待核是一个提示：仍继续做结构检查，以便给出精确原因
        # （停用成员 / 端点停用 / 端点反转）；只有结构上也查不出问题时，
        # 才回退为通用的待核标记。
        marked_pending = citation.get("status") == PENDING
        if citation["volume_id"] != volume_id:
            return RefExpansion(
                ref=ref,
                status=CROSS_VOLUME,
                kind=citation["kind"],
                citation_id=cid,
                pages=tuple(self._known_pages(citation)),
                opening_nos=(),
                provenance=tuple(self._known_provenance(citation)),
                detail={"citation_volume_id": citation["volume_id"]},
            )
        if citation["kind"] == PINNED:
            return self._expand_pinned(volume_id, revision_id, ref, citation,
                                       marked_pending=marked_pending)
        if citation["kind"] == RANGE:
            return self._expand_range(volume_id, revision_id, ref, citation,
                                      marked_pending=marked_pending)
        raise ValueError(f"unknown citation kind {citation['kind']!r}")

    def _expand_pinned(
        self,
        volume_id: str,
        revision_id: str,
        ref: dict[str, Any],
        citation: dict[str, Any],
        *,
        marked_pending: bool = False,
    ) -> RefExpansion:
        snapshot = self._snapshot(volume_id)
        cid = citation["citation_id"]
        members = [
            snapshot.pages[(volume_id, m["page_id"])]
            for m in citation["members"]
        ]
        provenance = tuple(
            ProvenanceEntry(
                volume_id=p.volume_id,
                page_id=p.page_id,
                label=p.label,
                section_id=p.section_id,
                role="pinned_member",
                citation_id=cid,
                note=("inactive" if not p.active else None),
            )
            for p in members
        )
        inactive = [p.page_id for p in members if not p.active]
        if inactive:
            return RefExpansion(
                ref=ref,
                status=PENDING,
                kind=PINNED,
                citation_id=cid,
                pages=tuple(members),
                opening_nos=(),
                provenance=provenance,
                detail={"reason": "pinned_inactive_member", "inactive_page_ids": inactive},
            )
        if marked_pending:
            # 登记待核但成员当前都在用：保留通用待核原因，不冒充可用。
            return RefExpansion(
                ref=ref, status=PENDING, kind=PINNED, citation_id=cid,
                pages=tuple(members), opening_nos=(), provenance=provenance,
                detail={"reason": "citation_marked_pending"},
            )
        return self._locate(volume_id, revision_id, ref, PINNED, cid, members, provenance, {})

    def _expand_range(
        self,
        volume_id: str,
        revision_id: str,
        ref: dict[str, Any],
        citation: dict[str, Any],
        *,
        marked_pending: bool = False,
    ) -> RefExpansion:
        snapshot = self._snapshot(volume_id)
        cid = citation["citation_id"]
        start = snapshot.pages[(volume_id, citation["start_page_id"])]
        end = snapshot.pages[(volume_id, citation["end_page_id"])]
        detail: dict[str, Any] = {
            "start_position": start.seq,
            "end_position": end.seq,
        }
        if not start.active or not end.active:
            detail["reason"] = "range_endpoint_inactive"
            detail["start_active"] = start.active
            detail["end_active"] = end.active
            provenance = (
                ProvenanceEntry(start.volume_id, start.page_id, start.label,
                                start.section_id, "range_start", cid,
                                "inactive" if not start.active else None),
                ProvenanceEntry(end.volume_id, end.page_id, end.label,
                                end.section_id, "range_end", cid,
                                "inactive" if not end.active else None),
            )
            return RefExpansion(
                ref=ref, status=PENDING, kind=RANGE, citation_id=cid,
                pages=(start, end), opening_nos=(), provenance=provenance, detail=detail,
            )
        # 端点反转：保留原来源顺序，不交换、不猜测；按物理顺序规范化仅用于
        # 说明实际区间，并在 provenance 上注明 reversed。
        reversed_order = start.seq > end.seq
        detail["reversed"] = reversed_order
        if reversed_order:
            detail["reason"] = "range_endpoints_reversed"
            lo, hi = end, start
            provenance = (
                ProvenanceEntry(start.volume_id, start.page_id, start.label,
                                start.section_id, "range_start", cid,
                                "source_order_before_physical_endpoint"),
                ProvenanceEntry(end.volume_id, end.page_id, end.label,
                                end.section_id, "range_end", cid,
                                "source_order_before_physical_startpoint"),
            )
            return RefExpansion(
                ref=ref, status=PENDING, kind=RANGE, citation_id=cid,
                pages=(start, end), opening_nos=(), provenance=provenance, detail=detail,
            )
        lo, hi = start, end
        interior = [
            p for p in snapshot.physical_order
            if lo.seq <= p.seq <= hi.seq and p.active
        ]
        provenance = tuple(self._range_provenance(cid, lo, hi, interior))
        if marked_pending:
            # 端点都在用且次序正常，但登记仍是待核：给出通用待核原因。
            return RefExpansion(
                ref=ref, status=PENDING, kind=RANGE, citation_id=cid,
                pages=tuple(interior), opening_nos=(),
                provenance=provenance,
                detail={"reason": "citation_marked_pending", **detail},
            )
        return self._locate(
            volume_id, revision_id, ref, RANGE, cid, interior, provenance, detail
        )

    def _expand_inline(
        self, volume_id: str, revision_id: str, ref: dict[str, Any]
    ) -> RefExpansion:
        snapshot = self._snapshot(volume_id)
        if "page_id" in ref:
            key = (volume_id, ref["page_id"])
            page = snapshot.pages.get(key)
            if page is None:
                return RefExpansion(
                    ref=ref, status=RESOLUTION_NOT_FOUND, kind="INLINE",
                    citation_id=None, pages=(), opening_nos=(), provenance=(),
                    detail={"page_id": ref["page_id"]},
                )
            if not page.active:
                return RefExpansion(
                    ref=ref, status=PENDING, kind="INLINE", citation_id=None,
                    pages=(page,), opening_nos=(),
                    provenance=(ProvenanceEntry(
                        page.volume_id, page.page_id, page.label,
                        page.section_id, "inline", None, "inactive"),),
                    detail={"reason": "inline_page_inactive"},
                )
            provenance = (ProvenanceEntry(
                page.volume_id, page.page_id, page.label,
                page.section_id, "inline", None),)
            return self._locate(
                volume_id, revision_id, ref, "INLINE", None, [page], provenance, {}
            )
        # 按 (section, label) 在**在用页**里解析，必须恰好一个。
        section_id = ref.get("section_id")
        label = str(ref.get("label"))
        matches = [
            p for p in snapshot.physical_order
            if p.active and p.section_id == section_id and p.label == label
        ]
        if not matches:
            return RefExpansion(
                ref=ref, status=RESOLUTION_NOT_FOUND, kind="INLINE",
                citation_id=None, pages=(), opening_nos=(), provenance=(),
                detail={"section_id": section_id, "label": label},
            )
        if len(matches) > 1:
            return RefExpansion(
                ref=ref, status=RESOLUTION_AMBIGUOUS, kind="INLINE",
                citation_id=None, pages=tuple(matches), opening_nos=(),
                provenance=(),
                detail={
                    "section_id": section_id,
                    "label": label,
                    "matching_page_ids": [p.page_id for p in matches],
                },
            )
        page = matches[0]
        provenance = (ProvenanceEntry(
            page.volume_id, page.page_id, page.label,
            page.section_id, "inline", None,
            note=f"resolved_from={section_id}/{label}"),)
        return self._locate(
            volume_id, revision_id, ref, "INLINE", None, [page], provenance, {}
        )

    # ------------------------------------------------------------------
    def _locate(
        self,
        volume_id: str,
        revision_id: str,
        ref: dict[str, Any],
        kind: str,
        cid: str | None,
        pages: list[PageRecord],
        provenance: tuple[ProvenanceEntry, ...],
        detail: dict[str, Any],
    ) -> RefExpansion:
        snapshot = self._snapshot(volume_id)
        opening_nos: list[int] = []
        for page in pages:
            opening = snapshot.opening_of(revision_id, page.as_key())
            if opening is None:
                # 在用页却不在本修订布局里：布局与快照不一致，按结构错误上报。
                return RefExpansion(
                    ref=ref, status=PENDING, kind=kind, citation_id=cid,
                    pages=tuple(pages), opening_nos=(), provenance=provenance,
                    detail={**detail, "reason": "page_absent_from_revision",
                            "page_id": page.page_id},
                )
            opening_nos.append(opening.opening_no)
        return RefExpansion(
            ref=ref, status=OK, kind=kind, citation_id=cid,
            pages=tuple(pages), opening_nos=tuple(opening_nos),
            provenance=provenance, detail=detail,
        )

    @staticmethod
    def _range_provenance(
        cid: str,
        start: PageRecord,
        end: PageRecord,
        interior: list[PageRecord],
    ) -> list[ProvenanceEntry]:
        entries: list[ProvenanceEntry] = []
        for page in interior:
            if page.as_key() == start.as_key():
                role = "range_start"
            elif page.as_key() == end.as_key():
                role = "range_end"
            else:
                role = "range_interior"
            entries.append(ProvenanceEntry(
                page.volume_id, page.page_id, page.label,
                page.section_id, role, cid))
        return entries

    def _known_pages(self, citation: dict[str, Any]) -> list[PageRecord]:
        vid = citation["volume_id"]
        snapshot = self.snapshots.get(vid)
        if snapshot is None:
            return []
        ids: list[str]
        if citation["kind"] == PINNED:
            ids = [m["page_id"] for m in citation.get("members", [])]
        else:
            ids = [citation.get("start_page_id"), citation.get("end_page_id")]
        return [
            snapshot.pages[(vid, pid)]
            for pid in ids
            if (vid, pid) in snapshot.pages
        ]

    def _known_provenance(
        self, citation: dict[str, Any]
    ) -> list[ProvenanceEntry]:
        cid = citation["citation_id"]
        return [
            ProvenanceEntry(
                p.volume_id, p.page_id, p.label, p.section_id,
                "pinned_member" if citation["kind"] == PINNED else "range_endpoint",
                cid,
                note=("inactive" if not p.active else None),
            )
            for p in self._known_pages(citation)
        ]


def _stable_ref_key(ref: dict[str, Any]) -> str:
    import json

    return json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
