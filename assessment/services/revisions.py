"""
责任区/合同修订服务：提案 -> 冲突校验 -> 影响预览 -> 确认发布 / 撤回 / 替代。

核心规则：
* 修订只登记提案（基准快照 + 修订内容 + 有效时间），确认发布前不改任何归属；
  未锁定项目也只先形成“待确认调整”（RevisionImpactItem applied=False）；
* 同一时空区间不得发布两个责任归属：合同区间重叠 / 网格面面积重叠一律 409；
* 确认发布在单个事务内完成（锁修订行 + 锁相关网格行 + 复查冲突 + 重算影响 +
  应用登记变更 + 追加审计版本），任何失败整体回滚，不留半套数据；
* 追溯修订命中未锁定(draft)处罚：确认后改事件/处罚归属快照，并追加 revision 版本留痕；
* 命中已锁定(locked)处罚：保留原合同/承包商快照，只追加 revision 版本（审计链），
  处罚回到待复核，由复核重新锁定；历史行不改写。
"""
import json
from dataclasses import dataclass

from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.db import transaction

from assessment.exceptions import (
    DuplicateRevision,
    InvalidRevision,
    RevisionConflict,
    RevisionNotActionable,
    UnresolvedAttribution,
)
from assessment.models import (
    CleaningContract,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    ResponsibilityRevision,
    RevisionImpactItem,
    RoadGrid,
)
from assessment.services.clock import Clock, SystemClock
from assessment.services.penalties import append_version

# 面积容差（度^2）：网格仅边界相接不算重叠，有面积交集才算冲突
AREA_TOLERANCE = 1e-12

PENDING = ResponsibilityRevision.Status.PENDING
PUBLISHED = ResponsibilityRevision.Status.PUBLISHED


@dataclass
class _ComputedItem:
    """影响计算结果（未落库）。"""

    item_kind: str
    disposition: str
    event: ProblemEvent | None = None
    penalty: PenaltyUnit | None = None
    photo: EvidencePhoto | None = None
    old_grid: RoadGrid | None = None
    new_grid: RoadGrid | None = None
    old_contract: CleaningContract | None = None
    new_contract: CleaningContract | None = None
    old_contract_code: str = ""
    new_contract_code: str = ""
    old_contractor_name: str = ""
    new_contractor_name: str = ""
    note: str = ""


# ---------------------------------------------------------------- 基础校验

def _parse_polygon(raw) -> Polygon:
    """把 GeoJSON（dict 或 str）解析为 SRID4326 多边形，非法一律 400。"""
    if raw is None:
        raise InvalidRevision("缺少网格范围 geom")
    try:
        geom = GEOSGeometry(raw if isinstance(raw, str) else json.dumps(raw))
    except Exception as exc:  # GEOSException / ValueError / TypeError
        raise InvalidRevision(f"网格范围不是合法的 GeoJSON: {exc}")
    if not isinstance(geom, Polygon):
        raise InvalidRevision("网格范围必须是 Polygon")
    if geom.srid is None:
        geom.srid = 4326
    if geom.srid != 4326:
        geom = geom.transform(4326, clone=True)
    if not geom.valid:
        raise InvalidRevision(f"多边形不自洽: {geom.valid_reason}")
    return geom


def _grid_baseline(grid: RoadGrid) -> dict:
    return {
        "grid_id": grid.id,
        "code": grid.code,
        "name": grid.name,
        "geom": json.loads(grid.geom.geojson),
    }


def _contract_baseline(contract: CleaningContract | None) -> dict:
    if contract is None:
        return {"contract": None, "note": "新增合同，无基准登记"}
    return {
        "contract_id": contract.id,
        "code": contract.code,
        "grid_id": contract.grid_id,
        "contractor_name": contract.contractor_name,
        "valid_from": contract.valid_from.isoformat(),
        "valid_to": contract.valid_to.isoformat(),
    }


def validate_revision_conflicts(revision: ResponsibilityRevision) -> None:
    """
    时空冲突校验：同一时空区间不得发布两个责任归属。
    提案、预览、确认发布三个阶段都会执行（确认时在行锁内复查，杜绝并发发布）。
    """
    if revision.target_kind == ResponsibilityRevision.TargetKind.CONTRACT_INTERVAL:
        qs = CleaningContract.objects.filter(
            grid_id=revision.grid_id,
            valid_from__lt=revision.effective_to,
            valid_to__gt=revision.effective_from,
        )
        if revision.target_contract_id:
            qs = qs.exclude(pk=revision.target_contract_id)
        conflicts = list(qs)
        if conflicts:
            codes = "、".join(c.code for c in conflicts)
            raise RevisionConflict(
                f"合同区间 [{revision.effective_from}, {revision.effective_to}) "
                f"与网格 {revision.grid.code} 已登记合同重叠: {codes}"
            )
    else:  # GRID_BOUNDARY
        new_geom = _parse_polygon(revision.proposed_payload.get("geom"))
        for other in RoadGrid.objects.exclude(pk=revision.grid_id):
            if other.geom.intersects(new_geom) and other.geom.intersection(new_geom).area > AREA_TOLERANCE:
                raise RevisionConflict(f"新边界与网格 {other.code} 存在面积重叠，会导致同一地点双重归属")


def _assert_pending(revision: ResponsibilityRevision, action: str) -> None:
    if revision.status != PENDING:
        raise RevisionNotActionable(
            f"修订 {revision.revision_no} 当前状态为“{revision.get_status_display()}”，不能{action}"
        )


# ---------------------------------------------------------------- 提案

@transaction.atomic
def create_revision(
    *,
    target_kind: str,
    grid: RoadGrid,
    proposed_payload: dict,
    effective_from,
    effective_to=None,
    target_contract: CleaningContract | None = None,
    reason: str = "",
    actor: str = "system",
    idempotency_key: str | None = None,
    clock: Clock | None = None,
) -> ResponsibilityRevision:
    """登记修订提案（pending）。只建提案与基准快照，不改任何归属。"""
    if idempotency_key:
        if ResponsibilityRevision.objects.filter(idempotency_key=idempotency_key).exists():
            raise DuplicateRevision(f"幂等键 {idempotency_key} 的修订已存在")
    if effective_to is not None and effective_to <= effective_from:
        raise InvalidRevision("生效结束时间必须晚于生效时间")

    if target_kind == ResponsibilityRevision.TargetKind.GRID_BOUNDARY:
        new_geom = _parse_polygon(proposed_payload.get("geom"))
        payload = {"geom": json.loads(new_geom.geojson)}
        baseline = _grid_baseline(grid)
        target_contract = None
    elif target_kind == ResponsibilityRevision.TargetKind.CONTRACT_INTERVAL:
        contractor = (proposed_payload.get("contractor_name") or "").strip()
        if not contractor:
            raise InvalidRevision("合同修订必须给出 contractor_name")
        if effective_to is None:
            raise InvalidRevision("合同修订必须给出 effective_to（合同区间结束时间）")
        if target_contract is not None and target_contract.grid_id != grid.id:
            raise InvalidRevision("被修订合同不属于目标网格")
        payload = {"contractor_name": contractor}
        if target_contract is None:
            code = (proposed_payload.get("code") or "").strip()
            if not code:
                raise InvalidRevision("新增合同修订必须给出合同编号 code")
            if CleaningContract.objects.filter(code=code).exists():
                raise RevisionConflict(f"合同编号 {code} 已存在")
            payload["code"] = code
        baseline = _contract_baseline(target_contract)
    else:
        raise InvalidRevision(f"未知修订类型 {target_kind}")

    revision = ResponsibilityRevision(
        target_kind=target_kind,
        grid=grid,
        target_contract=target_contract,
        baseline_snapshot=baseline,
        proposed_payload=payload,
        effective_from=effective_from,
        effective_to=effective_to,
        reason=reason,
        idempotency_key=idempotency_key or None,
        created_by=actor,
    )
    validate_revision_conflicts(revision)
    revision.save()
    return revision


# ---------------------------------------------------------------- 影响计算

def _resolve_proposed(point, at, revised_grid, new_geom, other_grids):
    """
    在“提议世界”（revised_grid 的边界被 new_geom 替换）下解析某点某时刻的
    网格与责任合同。返回 (grid, contract, note)；无法唯一解析时 contract 为 None。
    """
    covering = []
    if new_geom.covers(point):
        covering.append(revised_grid)
    covering.extend(g for g in other_grids if g.geom.covers(point))
    if not covering:
        return None, None, "修订后位置不属于任何网格"
    if len(covering) > 1:
        return None, None, "修订后位置被多个网格覆盖，归属不唯一"
    grid = covering[0]
    contracts = list(
        CleaningContract.objects.filter(grid=grid, valid_from__lte=at, valid_to__gt=at)[:2]
    )
    if not contracts:
        return grid, None, f"修订后网格 {grid.code} 在发生时刻无生效合同"
    if len(contracts) > 1:
        return grid, None, f"修订后网格 {grid.code} 在发生时刻存在重叠合同"
    return grid, contracts[0], ""


def _event_penalty_disposition(penalty: PenaltyUnit | None, resolved: bool) -> str:
    if penalty is not None and penalty.status == PenaltyUnit.Status.LOCKED:
        return RevisionImpactItem.Disposition.LOCKED_AUDIT
    if not resolved:
        return RevisionImpactItem.Disposition.UNRESOLVED
    return RevisionImpactItem.Disposition.REASSIGN_PENDING


def _compute_impact_contract(revision: ResponsibilityRevision) -> list[_ComputedItem]:
    """合同区间修订：影响 网格 × [effective_from, effective_to) 内的事件/照片。"""
    grid = revision.grid
    f, t = revision.effective_from, revision.effective_to
    contractor = revision.proposed_payload["contractor_name"]
    if revision.target_contract_id:
        new_code = revision.target_contract.code
    else:
        new_code = revision.proposed_payload["code"]

    items: list[_ComputedItem] = []
    events = (
        ProblemEvent.objects.filter(
            location__coveredby=grid.geom,
            occurred_at__gte=f,
            occurred_at__lt=t,
        )
        .select_related("penalty", "contract", "grid")
        .order_by("pk")
    )
    for event in events:
        # 冲突校验已保证 [f,t) 内本网格只有修订合同，故修订后必归该合同
        if (
            event.contractor_name == contractor
            and event.contract is not None
            and event.contract.code == new_code
        ):
            continue  # 归属不变，不算受影响
        penalty = getattr(event, "penalty", None)
        disposition = _event_penalty_disposition(penalty, resolved=True)
        common = dict(
            old_grid=event.grid,
            new_grid=grid,
            old_contract=event.contract,
            new_contract=revision.target_contract,
            old_contract_code=event.contract.code if event.contract_id else "",
            new_contract_code=new_code,
            old_contractor_name=event.contractor_name,
            new_contractor_name=contractor,
        )
        items.append(_ComputedItem(
            RevisionImpactItem.ItemKind.EVENT, disposition, event=event, **common,
        ))
        if penalty is not None:
            items.append(_ComputedItem(
                RevisionImpactItem.ItemKind.PENALTY, disposition, event=event, penalty=penalty, **common,
            ))

    photos = (
        EvidencePhoto.objects.filter(
            location__coveredby=grid.geom,
            captured_at__gte=f,
            captured_at__lt=t,
        )
        .select_related("event")
        .order_by("pk")
    )
    for photo in photos:
        items.append(_ComputedItem(
            RevisionImpactItem.ItemKind.PHOTO,
            RevisionImpactItem.Disposition.PHOTO_CONTEXT,
            event=photo.event,
            photo=photo,
            old_grid=grid,
            new_grid=grid,
            old_contractor_name=photo.event.contractor_name if photo.event_id else "",
            new_contractor_name=contractor,
            note="照片落在修订时空范围内，归属上下文随之变化",
        ))
    return items


def _compute_impact_grid(revision: ResponsibilityRevision) -> list[_ComputedItem]:
    """边界修订：影响 发生/拍摄时间在有效窗口内 且 覆盖关系发生变化 的事件/照片。"""
    grid = revision.grid
    new_geom = _parse_polygon(revision.proposed_payload.get("geom"))
    f, t = revision.effective_from, revision.effective_to
    union = grid.geom.union(new_geom)
    other_grids = list(RoadGrid.objects.exclude(pk=grid.pk))

    def in_window(qs, field):
        qs = qs.filter(**{f"{field}__gte": f})
        if t is not None:
            qs = qs.filter(**{f"{field}__lt": t})
        return qs

    items: list[_ComputedItem] = []
    events = in_window(
        ProblemEvent.objects.filter(location__coveredby=union), "occurred_at"
    ).select_related("penalty", "contract", "grid").order_by("pk")
    for event in events:
        point = event.location
        if grid.geom.covers(point) == new_geom.covers(point):
            continue  # 覆盖关系未变，不受影响
        new_grid, new_contract, note = _resolve_proposed(
            point, event.occurred_at, grid, new_geom, other_grids
        )
        penalty = getattr(event, "penalty", None)
        disposition = _event_penalty_disposition(penalty, resolved=new_contract is not None)
        common = dict(
            old_grid=event.grid,
            new_grid=new_grid,
            old_contract=event.contract,
            new_contract=new_contract,
            old_contract_code=event.contract.code if event.contract_id else "",
            new_contract_code=new_contract.code if new_contract else "",
            old_contractor_name=event.contractor_name,
            new_contractor_name=new_contract.contractor_name if new_contract else "",
            note=note,
        )
        items.append(_ComputedItem(
            RevisionImpactItem.ItemKind.EVENT, disposition, event=event, **common,
        ))
        if penalty is not None:
            items.append(_ComputedItem(
                RevisionImpactItem.ItemKind.PENALTY, disposition, event=event, penalty=penalty, **common,
            ))

    photos = in_window(
        EvidencePhoto.objects.filter(location__coveredby=union), "captured_at"
    ).select_related("event").order_by("pk")
    for photo in photos:
        point = photo.location
        if grid.geom.covers(point) == new_geom.covers(point):
            continue
        items.append(_ComputedItem(
            RevisionImpactItem.ItemKind.PHOTO,
            RevisionImpactItem.Disposition.PHOTO_CONTEXT,
            event=photo.event,
            photo=photo,
            old_grid=photo.event.grid if photo.event_id else None,
            note="照片位置的网格覆盖关系因边界修订而变化",
        ))
    return items


def _compute_impact(revision: ResponsibilityRevision) -> list[_ComputedItem]:
    if revision.target_kind == ResponsibilityRevision.TargetKind.CONTRACT_INTERVAL:
        return _compute_impact_contract(revision)
    return _compute_impact_grid(revision)


def _summarize(items: list[_ComputedItem]) -> dict:
    summary = {
        "events": 0, "penalties": 0, "photos": 0,
        "reassign_pending": 0, "locked_audit": 0, "unresolved": 0,
    }
    for item in items:
        if item.item_kind == RevisionImpactItem.ItemKind.EVENT:
            summary["events"] += 1
        elif item.item_kind == RevisionImpactItem.ItemKind.PENALTY:
            summary["penalties"] += 1
        elif item.item_kind == RevisionImpactItem.ItemKind.PHOTO:
            summary["photos"] += 1
        if item.disposition == RevisionImpactItem.Disposition.REASSIGN_PENDING:
            summary["reassign_pending"] += 1
        elif item.disposition == RevisionImpactItem.Disposition.LOCKED_AUDIT:
            summary["locked_audit"] += 1
        elif item.disposition == RevisionImpactItem.Disposition.UNRESOLVED:
            summary["unresolved"] += 1
    return summary


def _replace_items(revision: ResponsibilityRevision, items: list[_ComputedItem], *, applied: bool, applied_at=None):
    """用一次影响计算结果整体替换修订的明细（预览可重复执行，不产生重复行）。"""
    RevisionImpactItem.objects.filter(revision=revision).delete()
    RevisionImpactItem.objects.bulk_create([
        RevisionImpactItem(
            revision=revision,
            item_kind=item.item_kind,
            disposition=item.disposition,
            event=item.event,
            penalty=item.penalty,
            photo=item.photo,
            old_grid=item.old_grid,
            new_grid=item.new_grid,
            old_contract=item.old_contract,
            new_contract=item.new_contract,
            old_contract_code=item.old_contract_code,
            new_contract_code=item.new_contract_code,
            old_contractor_name=item.old_contractor_name,
            new_contractor_name=item.new_contractor_name,
            applied=applied,
            applied_at=applied_at,
            note=item.note,
        )
        for item in items
    ])


# ---------------------------------------------------------------- 预览

@transaction.atomic
def preview_revision(
    revision: ResponsibilityRevision, *, actor: str = "system", clock: Clock | None = None
) -> ResponsibilityRevision:
    """
    影响预览：精确计算受影响的照片/事件/处罚归属并落库为待确认调整。
    可重复执行（整体替换旧明细），确认前不改任何归属。
    """
    clock = clock or SystemClock()
    revision = ResponsibilityRevision.objects.select_for_update().get(pk=revision.pk)
    _assert_pending(revision, "预览")
    validate_revision_conflicts(revision)
    items = _compute_impact(revision)
    _replace_items(revision, items, applied=False)
    revision.impact_summary = _summarize(items)
    revision.previewed_at = clock.now()
    revision.save(update_fields=["impact_summary", "previewed_at", "updated_at"])
    return revision


# ---------------------------------------------------------------- 确认发布

def _lock_related_grids(revision: ResponsibilityRevision) -> None:
    """发布串行化：锁定相关网格行，防止两个修订并发发布造成半套数据。"""
    if revision.target_kind == ResponsibilityRevision.TargetKind.GRID_BOUNDARY:
        # 边界修订影响全局空间关系：按主键顺序锁全部网格，避免交错死锁
        list(RoadGrid.objects.select_for_update().order_by("pk"))
    else:
        RoadGrid.objects.select_for_update().get(pk=revision.grid_id)


def _apply_registry_change(revision: ResponsibilityRevision) -> CleaningContract | None:
    """应用登记变更本身，返回（可能的）合同行。"""
    if revision.target_kind == ResponsibilityRevision.TargetKind.GRID_BOUNDARY:
        grid = RoadGrid.objects.get(pk=revision.grid_id)
        grid.geom = _parse_polygon(revision.proposed_payload.get("geom"))
        grid.save(update_fields=["geom"])
        return None
    contractor = revision.proposed_payload["contractor_name"]
    if revision.target_contract_id:
        contract = CleaningContract.objects.select_for_update().get(pk=revision.target_contract_id)
        contract.contractor_name = contractor
        contract.valid_from = revision.effective_from
        contract.valid_to = revision.effective_to
        contract.save(update_fields=["contractor_name", "valid_from", "valid_to", "updated_at"])
        return contract
    return CleaningContract.objects.create(
        code=revision.proposed_payload["code"],
        grid=revision.grid,
        contractor_name=contractor,
        valid_from=revision.effective_from,
        valid_to=revision.effective_to,
    )


def _apply_event_reassign(revision, item: _ComputedItem, contract_override, actor: str) -> None:
    """未锁定：确认后改事件/处罚归属快照，并追加 revision 版本留痕。"""
    event = ProblemEvent.objects.select_for_update().get(pk=item.event.pk)
    new_contract = item.new_contract or contract_override
    update_fields = ["contract", "contractor_name", "updated_at"]
    event.contract = new_contract
    event.contractor_name = item.new_contractor_name
    if item.new_grid is not None and item.new_grid.pk != event.grid_id:
        event.grid = item.new_grid
        update_fields.append("grid")
    event.save(update_fields=update_fields)

    penalty = getattr(event, "penalty", None)
    if penalty is None:
        return
    penalty.contract = new_contract
    penalty.contractor_name = item.new_contractor_name
    penalty.save(update_fields=["contract", "contractor_name", "updated_at"])
    append_version(
        penalty,
        points=penalty.points,
        escalation_level=penalty.escalation_level,
        kind=PenaltyVersion.Kind.REVISION,
        reason=(
            f"修订 {revision.revision_no} 确认发布：责任归属 "
            f"{item.old_contractor_name or '无'}（{item.old_contract_code or '无合同'}）→ "
            f"{item.new_contractor_name}（{item.new_contract_code}）"
        ),
        actor=actor,
    )


def _apply_locked_audit(revision, item: _ComputedItem, actor: str) -> None:
    """已锁定：保留原合同/承包商快照，只追加 revision 版本（审计链），待复核。"""
    penalty = item.penalty
    if penalty is None:
        return
    append_version(
        penalty,
        points=penalty.points,
        escalation_level=penalty.escalation_level,
        kind=PenaltyVersion.Kind.REVISION,
        reason=(
            f"修订 {revision.revision_no}：处罚已锁定，保留原归属快照 "
            f"{item.old_contractor_name}（{item.old_contract_code or '无合同'}）；"
            f"更正归属 {item.new_contractor_name or '待定'}（{item.new_contract_code or '无合同'}）"
            f"记入审计链，待复核"
        ),
        actor=actor,
    )


@transaction.atomic
def confirm_revision(
    revision: ResponsibilityRevision, *, actor: str = "system", clock: Clock | None = None
) -> ResponsibilityRevision:
    """
    确认发布：锁内复查冲突 -> 重算影响 -> 应用登记变更与归属调整 -> 置为已发布。
    全程单事务，任何失败整体回滚；重复发布/并发冲突一律 409。
    """
    clock = clock or SystemClock()
    revision = ResponsibilityRevision.objects.select_for_update().get(pk=revision.pk)
    _assert_pending(revision, "确认发布")
    _lock_related_grids(revision)
    validate_revision_conflicts(revision)

    items = _compute_impact(revision)
    unresolved = [i for i in items if i.disposition == RevisionImpactItem.Disposition.UNRESOLVED]
    if unresolved:
        raise UnresolvedAttribution(
            f"修订后 {len(unresolved)} 个未锁定事件无法归属（如事件 "
            f"{unresolved[0].event.event_no}），需先补齐合同或调整边界"
        )

    applied_contract = _apply_registry_change(revision)
    now = clock.now()
    if applied_contract is not None:
        # 新增合同的情形：影响明细补挂实际合同行
        for item in items:
            if item.new_contract is None:
                item.new_contract = applied_contract
    for item in items:
        # 事件条目驱动归属调整；处罚条目驱动锁定处罚的审计链追加
        if item.item_kind == RevisionImpactItem.ItemKind.EVENT:
            if item.disposition == RevisionImpactItem.Disposition.REASSIGN_PENDING:
                _apply_event_reassign(revision, item, applied_contract, actor)
        elif item.item_kind == RevisionImpactItem.ItemKind.PENALTY:
            if item.disposition == RevisionImpactItem.Disposition.LOCKED_AUDIT:
                _apply_locked_audit(revision, item, actor)

    _replace_items(revision, items, applied=True, applied_at=now)
    revision.status = PUBLISHED
    revision.published_at = now
    revision.published_by = actor
    revision.impact_summary = _summarize(items)
    revision.save(update_fields=[
        "status", "published_at", "published_by", "impact_summary", "updated_at",
    ])
    return revision


# ---------------------------------------------------------------- 撤回 / 替代

@transaction.atomic
def withdraw_revision(
    revision: ResponsibilityRevision, *, actor: str = "system", clock: Clock | None = None
) -> ResponsibilityRevision:
    """撤回：仅待确认状态可撤回；已发布的修订效果已生效，只能再提新修订替代。"""
    clock = clock or SystemClock()
    revision = ResponsibilityRevision.objects.select_for_update().get(pk=revision.pk)
    _assert_pending(revision, "撤回")
    revision.status = ResponsibilityRevision.Status.WITHDRAWN
    revision.withdrawn_at = clock.now()
    revision.withdrawn_by = actor
    revision.save(update_fields=["status", "withdrawn_at", "withdrawn_by", "updated_at"])
    return revision


@transaction.atomic
def supersede_revision(
    revision: ResponsibilityRevision, *, actor: str = "system", clock: Clock | None = None,
    **new_payload,
) -> ResponsibilityRevision:
    """
    替代：以一份新提案取代旧修订。
    * 旧修订为待确认：标记为已被替代，不得再发布；
    * 旧修订为已发布：登记效果保留，仅标记被新修订替代（审计链延续）。
    """
    clock = clock or SystemClock()
    revision = ResponsibilityRevision.objects.select_for_update().get(pk=revision.pk)
    if revision.status not in (PENDING, PUBLISHED):
        raise RevisionNotActionable(
            f"修订 {revision.revision_no} 当前状态为“{revision.get_status_display()}”，不能被替代"
        )
    new_revision = create_revision(actor=actor, clock=clock, **new_payload)
    revision.status = ResponsibilityRevision.Status.SUPERSEDED
    revision.save(update_fields=["status", "updated_at"])
    new_revision.supersedes = revision
    new_revision.save(update_fields=["supersedes", "updated_at"])
    return new_revision
