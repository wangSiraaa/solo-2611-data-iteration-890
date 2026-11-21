"""目录修订与台型：把在用页按成对规则排成对开（opening）。

一页对开（opening，也称一个"面"）是读谱台一拍内唯一能同时显示的物理
单元，编号（``opening_no``）从 1 起。台型只决定**在用页**如何成对；停用
页不占面，但仍保留其稳定身份与物理位置（见 :mod:`citation_system.catalog`）。

两种台型：

* ``paired``（默认）：第 1、2 页同面，第 3、4 页同面，……末面可单页。
* ``leading_blank``：卷首先放一个显示空位，第 1 页独占首面，之后成对，
  末面可单页。

只凭纸上印刷页码（label）会把同号附页排错，因此所有运算都以稳定身份
``(volume_id, page_id)`` 为准；label 只作展示与按文字解析之用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STAND_PAIRED = "paired"
STAND_LEADING_BLANK = "leading_blank"
STAND_TYPES = (STAND_PAIRED, STAND_LEADING_BLANK)


@dataclass(frozen=True)
class PageRecord:
    """一页的不可变记录，身份是 ``(volume_id, page_id)``。"""

    volume_id: str
    page_id: str
    section_id: str
    label: str
    seq: int
    active: bool

    def as_key(self) -> tuple[str, str]:
        return (self.volume_id, self.page_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume_id": self.volume_id,
            "page_id": self.page_id,
            "section_id": self.section_id,
            "label": self.label,
            "active": self.active,
            "seq": self.seq,
        }


@dataclass(frozen=True)
class Opening:
    """一个对开（一面）。``pages`` 含 1 或 2 个在用页，按在用次序排列。"""

    opening_no: int
    pages: tuple[PageRecord, ...]
    leading_blank: bool = False

    @property
    def page_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(p.as_key() for p in self.pages)

    def as_dict(self) -> dict[str, Any]:
        return {
            "opening_no": self.opening_no,
            "leading_blank": self.leading_blank,
            "pages": [p.as_dict() for p in self.pages],
        }


@dataclass(frozen=True)
class LayoutConfig:
    """一场排练固定的目录修订与台型。

    ``revision_id`` 是这份修订的稳定名字（例如 ``catA``）；
    ``catalog_revision`` 是来源目录的修订号，用于按版共享展开结果。
    ``active_order`` 显式给出本修订的在用页身份次序；停用页身份单列在
    ``inactive_page_ids``，不参与成对，但可被引用展开判为待处理。
    """

    volume_id: str
    revision_id: str
    stand_type: str = STAND_PAIRED
    active_order: tuple[tuple[str, str], ...] = ()
    inactive_page_ids: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    catalog_revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume_id": self.volume_id,
            "revision_id": self.revision_id,
            "stand_type": self.stand_type,
            "catalog_revision": self.catalog_revision,
            "active_order": [
                {"volume_id": v, "page_id": p} for v, p in self.active_order
            ],
            "inactive_page_ids": [
                {"volume_id": v, "page_id": p}
                for v, p in sorted(self.inactive_page_ids)
            ],
        }


@dataclass(frozen=True)
class VolumeSnapshot:
    """一卷某一版的不可变内存快照：页记录、修订布局、对开表。

    纯排练计算只依赖快照，不触碰 SQLite，这也是离线复核能成立的原因。
    """

    volume_id: str
    pages: dict[tuple[str, str], PageRecord]
    physical_order: tuple[PageRecord, ...]
    layouts: dict[str, list[Opening]]
    catalog_revision: int

    def opening_count(self, revision_id: str) -> int:
        return len(self.layouts[revision_id])

    def opening_of(
        self, revision_id: str, key: tuple[str, str]
    ) -> Opening | None:
        """该稳定页在本修订下所在的对开；停用页或非本卷页返回 None。"""

        for opening in self.layouts[revision_id]:
            if key in opening.page_keys:
                return opening
        return None

    def opening(self, revision_id: str, opening_no: int) -> Opening:
        return self.layouts[revision_id][opening_no - 1]

    def label_index(self) -> dict[tuple[str, str], list[PageRecord]]:
        index: dict[tuple[str, str], list[PageRecord]] = {}
        for page in self.physical_order:
            index.setdefault((page.section_id, page.label), []).append(page)
        return index


def build_layout(config: LayoutConfig, pages: dict[tuple[str, str], PageRecord]) -> list[Opening]:
    """按台型把修订的在用次序成对切成对开。"""

    if config.stand_type not in STAND_TYPES:
        raise ValueError(f"unknown stand_type {config.stand_type!r}")
    openings: list[Opening] = []
    ordered = [pages[key] for key in config.active_order]
    no = 1
    if config.stand_type == STAND_LEADING_BLANK:
        # 卷首显示空位：第 1 页独占首面。
        openings.append(Opening(no, (), leading_blank=True))
        no += 1
        if ordered:
            openings.append(Opening(no, (ordered[0],)))
            no += 1
            rest = ordered[1:]
        else:
            rest = []
    else:
        rest = ordered
    for i in range(0, len(rest), 2):
        openings.append(Opening(no, tuple(rest[i : i + 2])))
        no += 1
    return openings


def snapshot_from_document(document: dict[str, Any]) -> dict[str, VolumeSnapshot]:
    """从离线文档（``rehearsal-bundle/1`` 片段）构造快照，不读数据库。

    文档形状::

        {"volumes": [{"volume_id", "catalog_revision", "pages": [PageRecord...],
                      "layouts": [LayoutConfig...]}]}
    """

    snapshots: dict[str, VolumeSnapshot] = {}
    for vol in document.get("volumes", []):
        vid = vol["volume_id"]
        page_records = [
            PageRecord(
                volume_id=vid,
                page_id=p["page_id"],
                section_id=p["section_id"],
                label=str(p["label"]),
                seq=int(p["seq"]),
                active=bool(p["active"]),
            )
            for p in vol["pages"]
        ]
        pages = {p.as_key(): p for p in page_records}
        physical = tuple(sorted(page_records, key=lambda p: p.seq))
        layouts: dict[str, list[Opening]] = {}
        for layout in vol["layouts"]:
            config = LayoutConfig(
                volume_id=vid,
                revision_id=layout["revision_id"],
                stand_type=layout.get("stand_type", STAND_PAIRED),
                active_order=tuple(
                    (item["volume_id"], item["page_id"])
                    for item in layout["active_order"]
                ),
                inactive_page_ids=frozenset(
                    (item["volume_id"], item["page_id"])
                    for item in layout.get("inactive_page_ids", [])
                ),
                catalog_revision=int(layout.get("catalog_revision", 0)),
            )
            layouts[config.revision_id] = build_layout(config, pages)
        snapshots[vid] = VolumeSnapshot(
            volume_id=vid,
            pages=pages,
            physical_order=physical,
            layouts=layouts,
            catalog_revision=int(vol.get("catalog_revision", 0)),
        )
    return snapshots


def snapshot_from_catalog(catalog: Any) -> dict[str, VolumeSnapshot]:
    """从一个已提交的 :class:`Catalog` 构造当前状态快照。

    快照在一次只读遍历中取齐所有页；调用方随后可在快照上自行声明若干
    目录修订（哪些在用、什么台型）。
    """

    snapshots: dict[str, VolumeSnapshot] = {}
    for vid in catalog.volume_ids():
        rows = catalog.pages(vid, include_inactive=True)
        page_records = [
            PageRecord(
                volume_id=vid,
                page_id=p.page_id,
                section_id=p.section_id,
                label=p.label,
                seq=p.seq,
                active=p.active,
            )
            for p in rows
        ]
        pages = {p.as_key(): p for p in page_records}
        snapshots[vid] = VolumeSnapshot(
            volume_id=vid,
            pages=pages,
            physical_order=tuple(page_records),
            layouts={},
            catalog_revision=catalog.revision(),
        )
    return snapshots


def layout_from_snapshot(
    snapshot: VolumeSnapshot,
    revision_id: str,
    *,
    stand_type: str = STAND_PAIRED,
    active_page_ids: list[str] | None = None,
) -> LayoutConfig:
    """便利构造：按快照当前在用页（或显式身份列表）声明一份修订布局。"""

    if active_page_ids is None:
        active = tuple(
            p.as_key() for p in snapshot.physical_order if p.active
        )
        inactive = frozenset(
            p.as_key() for p in snapshot.physical_order if not p.active
        )
    else:
        wanted = [(snapshot.volume_id, pid) for pid in active_page_ids]
        active = tuple(key for key in wanted if key in snapshot.pages)
        inactive = frozenset(
            p.as_key() for p in snapshot.physical_order if p.as_key() not in active
        )
    config = LayoutConfig(
        volume_id=snapshot.volume_id,
        revision_id=revision_id,
        stand_type=stand_type,
        active_order=active,
        inactive_page_ids=inactive,
        catalog_revision=snapshot.catalog_revision,
    )
    snapshot.layouts[revision_id] = build_layout(config, snapshot.pages)
    return config
