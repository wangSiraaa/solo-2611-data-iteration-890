"""离线排练排页（rehearsal paging）。

在既有稳定页身份与 PINNED/RANGE 引用仓之上，新增三层：

* :mod:`citation_system.rehearsal.model` -- 目录修订 + 台型如何把在用页
  成对排成对开（``Opening``），以及不可变的 ``VolumeSnapshot``。
* :mod:`citation_system.rehearsal.expansion` -- 把一拍指定的固定引用或
  范围展成该版实际须看见的页、对开和来源历（provenance），并区分待处理、
  跨卷、不能同显等情形。
* :mod:`citation_system.rehearsal.solver` -- 按声部的起始对开、正反翻页
  速率与可翻间隙表，求最少实际翻动距离与逐拍面号；支持整句替代谱本。

本子包的纯计算部分只依赖内存快照，不打开 SQLite，因此一份已封存的排练
（:mod:`citation_system.rehearsal.seal`）可以在断网台上原样复核。
"""

from .model import (
    LayoutConfig,
    Opening,
    PageRecord,
    VolumeSnapshot,
    build_layout,
    snapshot_from_catalog,
    snapshot_from_document,
)
from .expansion import (
    OK,
    CROSS_VOLUME,
    NOT_CO_DISPLAYABLE,
    PENDING,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_NOT_FOUND,
    ExpansionEngine,
)
from .solver import solve_part, solve_rehearsal
from .seal import seal_rehearsal, verify_bundle

__all__ = [
    "LayoutConfig",
    "Opening",
    "PageRecord",
    "VolumeSnapshot",
    "build_layout",
    "snapshot_from_catalog",
    "snapshot_from_document",
    "OK",
    "CROSS_VOLUME",
    "NOT_CO_DISPLAYABLE",
    "PENDING",
    "RESOLUTION_AMBIGUOUS",
    "RESOLUTION_NOT_FOUND",
    "ExpansionEngine",
    "solve_part",
    "solve_rehearsal",
    "seal_rehearsal",
    "verify_bundle",
]
