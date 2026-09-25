"""
修订“模拟世界”：在“假设提案已生效”的网格/合同状态下解析责任归属。

用于：
* 冲突校验（提案生效后是否产生区间断档 / 重叠 / 网格重叠）；
* 影响预览（精确找出受影响事件、照片、处罚及修订前后归属）。

纯只读：本模块不写库，发布动作（revisions.apply）才会真正落库。
"""
from dataclasses import dataclass, field
from datetime import datetime

from django.contrib.gis.geos import Point


@dataclass(frozen=True)
class WorldGrid:
    id: int | None
    geom: object
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    code: str = ""


@dataclass(frozen=True)
class WorldContract:
    id: int | None
    grid_id: int
    contractor_name: str
    valid_from: datetime
    valid_to: datetime

    def active_at(self, at: datetime) -> bool:
        return self.valid_from <= at < self.valid_to


@dataclass
class World:
    grids: dict = field(default_factory=dict)          # id -> WorldGrid（含被修订覆盖）
    contracts: dict = field(default_factory=dict)      # id -> WorldContract（含被修订覆盖）
    extra_grids: list = field(default_factory=list)    # 新建网格（id=None）
    extra_contracts: list = field(default_factory=list)  # 新建合同（id=None）

    def all_grids(self):
        yield from self.grids.values()
        yield from self.extra_grids

    def all_contracts(self):
        yield from self.contracts.values()
        yield from self.extra_contracts


class Unresolved:
    """解析失败原因。"""

    GAP = "gap"          # 修订后时刻没有生效责任归属
    OVERLAP = "overlap"  # 修订后时刻存在多个责任归属
    OUTSIDE = "outside"  # 点不在任何网格内

    def __init__(self, reason: str):
        self.reason = reason


def _grid_active(grid: WorldGrid, at: datetime) -> bool:
    if grid.effective_from is not None and at < grid.effective_from:
        return False
    if grid.effective_to is not None and at >= grid.effective_to:
        return False
    return True


def build_world(
    *,
    grid_overrides: dict | None = None,
    contract_overrides: dict | None = None,
    extra_grids: list | None = None,
    extra_contracts: list | None = None,
) -> World:
    """
    从当前库构造世界快照，并叠加提案覆盖值。

    grid_overrides: {grid_id: {"geom", "effective_from", "effective_to", "code", ...}}
    contract_overrides: {contract_id: WorldContract-like dict（含 grid_id）}
    """
    from assessment.models import CleaningContract, RoadGrid

    grids: dict = {}
    for g in RoadGrid.objects.all():
        values = grid_overrides.get(g.id) if grid_overrides else None
        if values is None:
            grids[g.id] = WorldGrid(
                id=g.id, geom=g.geom,
                effective_from=g.effective_from, effective_to=g.effective_to,
                code=g.code,
            )
        else:
            grids[g.id] = WorldGrid(
                id=g.id,
                geom=values.get("geom", g.geom),
                effective_from=values.get("effective_from", g.effective_from),
                effective_to=values.get("effective_to", g.effective_to),
                code=values.get("code", g.code),
            )

    contracts: dict = {}
    for c in CleaningContract.objects.all():
        values = contract_overrides.get(c.id) if contract_overrides else None
        if values is None:
            contracts[c.id] = WorldContract(
                id=c.id, grid_id=c.grid_id, contractor_name=c.contractor_name,
                valid_from=c.valid_from, valid_to=c.valid_to,
            )
        else:
            contracts[c.id] = WorldContract(
                id=c.id,
                grid_id=values.get("grid_id", c.grid_id),
                contractor_name=values.get("contractor_name", c.contractor_name),
                valid_from=values.get("valid_from", c.valid_from),
                valid_to=values.get("valid_to", c.valid_to),
            )

    return World(
        grids=grids,
        contracts=contracts,
        extra_grids=list(extra_grids or []),
        extra_contracts=list(extra_contracts or []),
    )


def covering_grids(world: World, point: Point, at: datetime) -> list:
    """点在 at 时刻被哪些生效网格覆盖（geom.covers 语义，与归属服务一致）。"""
    hits = []
    for grid in world.all_grids():
        if _grid_active(grid, at) and grid.geom.covers(point):
            hits.append(grid)
    return hits


def resolve_contract(world: World, point: Point, at: datetime):
    """
    在模拟世界中解析 at 时刻 point 的责任合同。

    返回 WorldContract；无网格 -> Unresolved(OUTSIDE)；
    断档 -> Unresolved(GAP)；多个 -> Unresolved(OVERLAP)。
    """
    grids = covering_grids(world, point, at)
    if not grids:
        return Unresolved(Unresolved.OUTSIDE)
    if len(grids) > 1:
        return Unresolved(Unresolved.OVERLAP)
    grid = grids[0]
    active = [
        c for c in world.all_contracts()
        if c.grid_id == grid.id and c.active_at(at)
    ]
    if not active:
        return Unresolved(Unresolved.GAP)
    if len(active) > 1:
        return Unresolved(Unresolved.OVERLAP)
    return active[0]


def intervals_overlap(start_a, end_a, start_b, end_b) -> bool:
    """半开区间 [start, end) 是否相交。"""
    return start_a < end_b and start_b < end_a
