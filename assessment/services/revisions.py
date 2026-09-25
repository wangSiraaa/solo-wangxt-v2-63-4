"""
责任区 / 合同修订服务：提案 → 冲突校验 → 影响预览 → 确认发布 / 撤回 / 替代。

铁律：
1. “按发生时归属”的历史不被直接覆盖：每次发布追加 GridHistory/ContractHistory；
2. 提案确认前只形成待确认调整（RevisionImpactItem），归属一律不改；
3. 同一时空区间不得存在两个责任归属（网格不重叠、同网格合同区间不重叠）；
4. 命中未锁定事件：确认后才改挂；命中已锁定处罚：原合同/承包商快照与锁定版本
   不动，只追加 attribution 版本 + AttributionCorrection 审计链；
5. 全部写操作在单事务内完成，任何校验失败整体回滚，不产生半套数据。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone

from django.db import transaction

from assessment.exceptions import (
    RevisionConflict,
    RevisionDuplicateSubmission,
    RevisionInvalid,
    RevisionNotDraft,
    RevisionStale,
    RevisionUnresolvedImpact,
)
from assessment.models import (
    AttributionCorrection,
    CleaningContract,
    ContractHistory,
    EvidencePhoto,
    GridHistory,
    PenaltyUnit,
    ProblemEvent,
    RevisionImpactItem,
    RevisionProposal,
    RoadGrid,
)
from assessment.services.clock import Clock, SystemClock
from assessment.services.penalties import append_attribution_version
from assessment.services import revision_world as rw


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class ImpactResult:
    """一次影响计算的结果（可用于无状态预览或填充提案影响项）。"""

    items: list = field(default_factory=list)        # list[dict]
    conflicts: list = field(default_factory=list)    # list[str] 时空冲突描述
    unresolved_count: int = 0
    unlocked_count: int = 0
    locked_count: int = 0
    future_count: int = 0
    photo_count: int = 0

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts) or self.unresolved_count > 0


# --------------------------------------------------------------------------- #
# 基础校验
# --------------------------------------------------------------------------- #

def _assert_valid_range(start, end, *, label: str) -> None:
    if start is not None and end is not None and not (start < end):
        raise RevisionInvalid(f"{label}：开始时间必须早于结束时间（半开区间 [start, end)）")


def _check_idempotency(idempotency_key: str | None) -> None:
    if idempotency_key and RevisionProposal.objects.filter(
        idempotency_key=idempotency_key
    ).exists():
        raise RevisionDuplicateSubmission(f"幂等键 {idempotency_key} 已存在，疑似重复提交")


def _grid_overlaps_other(geom, *, exclude_grid_id: int | None,
                         effective_from=None, effective_to=None) -> RoadGrid | None:
    """
    与其他网格的“同一时空区间”冲突：面相交且有效时间重叠。
    使用 ST_Intersects + 面积阈值判定面重叠（相切/共边不算）。
    NULL 有效时间视为负无穷 / 正无穷。
    """
    start = effective_from or _T_MIN
    end = effective_to or _T_MAX
    for other in RoadGrid.objects.exclude(id=exclude_grid_id):
        other_start = other.effective_from or _T_MIN
        other_end = other.effective_to or _T_MAX
        if not rw.intervals_overlap(start, end, other_start, other_end):
            continue
        if geom.intersection(other.geom).area > 1e-12:
            return other
    return None


_T_MIN = datetime(1900, 1, 1, tzinfo=dt_timezone.utc)
_T_MAX = datetime(2999, 12, 31, tzinfo=dt_timezone.utc)


def _check_contract_overlap(target_grid_id: int, start, end,
                            *, exclude_contract_id: int | None) -> CleaningContract | None:
    """同网格内合同责任区间不得重叠。"""
    qs = CleaningContract.objects.filter(grid_id=target_grid_id)
    if exclude_contract_id is not None:
        qs = qs.exclude(id=exclude_contract_id)
    for other in qs:
        if rw.intervals_overlap(start, end, other.valid_from, other.valid_to):
            return other
    return None


# --------------------------------------------------------------------------- #
# 提案构造（含基准快照）
# --------------------------------------------------------------------------- #

def _world_for_proposal(proposal: RevisionProposal) -> rw.World:
    """按提案内容构造“生效后”模拟世界。"""
    if proposal.target_type == RevisionProposal.Target.CONTRACT:
        target_grid = proposal.new_grid or proposal.grid
        override = {
            "grid_id": target_grid.id,
            "contractor_name": proposal.new_contractor_name or proposal.contract.contractor_name,
            "valid_from": proposal.new_valid_from,
            "valid_to": proposal.new_valid_to,
        }
        return rw.build_world(contract_overrides={proposal.contract_id: override})

    override = {"geom": proposal.new_geom}
    if proposal.effective_from is not None or proposal.effective_to is not None:
        override["effective_from"] = proposal.effective_from
        override["effective_to"] = proposal.effective_to
    return rw.build_world(grid_overrides={proposal.grid_id: override})


def _candidate_events(proposal: RevisionProposal):
    """可能落入修订时空范围的事件（空间粗筛 + 时间半开区间）。"""
    qs = ProblemEvent.objects.select_related("contract", "grid", "penalty")
    if proposal.target_type == RevisionProposal.Target.CONTRACT:
        qs = qs.filter(contract_id=proposal.contract_id)
    else:
        qs = qs.filter(grid_id=proposal.grid_id)
    start, end = proposal.effective_from, proposal.effective_to
    if start is not None:
        qs = qs.filter(occurred_at__gte=start)
    if end is not None:
        qs = qs.filter(occurred_at__lt=end)
    return qs


def _candidate_photos(proposal: RevisionProposal):
    """与受影响事件相关、或坐标在目标新网格内的证据照片（Python 合并去重）。"""
    event_ids = list(_candidate_events(proposal).values_list("id", flat=True))
    photo_ids = set(
        EvidencePhoto.objects.filter(event_id__in=event_ids).values_list("id", flat=True)
    )
    if proposal.target_type == RevisionProposal.Target.GRID:
        photo_ids.update(
            EvidencePhoto.objects.filter(
                location__coveredby=proposal.new_geom
            ).values_list("id", flat=True)
        )
    return EvidencePhoto.objects.filter(id__in=photo_ids).select_related("event")


def compute_impact(proposal: RevisionProposal, *, clock: Clock | None = None) -> ImpactResult:
    """在模拟世界中精确计算受影响事件 / 照片 / 处罚及修订前后归属。"""
    clock = clock or SystemClock()
    current_time = clock.now()
    world = _world_for_proposal(proposal)
    result = ImpactResult()

    # ---- 时空冲突：网格重叠 / 合同区间重叠 ----
    if proposal.target_type == RevisionProposal.Target.CONTRACT:
        target_grid = proposal.new_grid or proposal.grid
        overlap = _check_contract_overlap(
            target_grid.id, proposal.new_valid_from, proposal.new_valid_to,
            exclude_contract_id=proposal.contract_id,
        )
        if overlap is not None:
            result.conflicts.append(
                f"合同区间与 {overlap.code}（{overlap.valid_from:%Y-%m-%d %H:%M} ~ "
                f"{overlap.valid_to:%Y-%m-%d %H:%M}）重叠，同一时空不得有两个责任归属"
            )
    else:
        eff_from = proposal.effective_from
        eff_to = proposal.effective_to
        overlap = _grid_overlaps_other(
            proposal.new_geom, exclude_grid_id=proposal.grid_id,
            effective_from=eff_from, effective_to=eff_to,
        )
        if overlap is not None:
            result.conflicts.append(f"修订后网格与网格 {overlap.code} 在同一时空区间重叠")

    # ---- 逐事件解析修订后归属 ----
    for event in _candidate_events(proposal):
        point = event.location
        at = event.occurred_at
        resolved = rw.resolve_contract(world, point, at)

        from_contract_id = event.contract_id
        from_name = event.contractor_name
        from_grid_id = event.grid_id

        if isinstance(resolved, rw.Unresolved):
            disposition = RevisionImpactItem.Disposition.UNRESOLVED
            note = {
                rw.Unresolved.GAP: "修订后该发生时刻在网格内没有生效合同（责任断档）",
                rw.Unresolved.OVERLAP: "修订后该发生时刻存在多个生效合同/网格（责任重叠）",
                rw.Unresolved.OUTSIDE: "修订后该位置不再属于任何网格",
            }[resolved.reason]
            to_contract_id, to_name, to_grid_id = None, "", None
            result.unresolved_count += 1
        elif at > current_time:
            # 未来修订只影响新事件：发生在当前时间之后的事件不做历史改挂
            disposition = RevisionImpactItem.Disposition.FUTURE
            note = "事件发生在未来；修订自生效起只影响新事件，本事件归属不动"
            to_contract_id, to_name, to_grid_id = resolved.id, resolved.contractor_name, resolved.grid_id
            result.future_count += 1
        elif resolved.id == from_contract_id and resolved.contractor_name == from_name:
            # 归属未变化（承包商改名也算变化，因此同时比较名称）
            continue
        else:
            penalty = getattr(event, "penalty", None)
            if penalty is not None and penalty.status == PenaltyUnit.Status.LOCKED:
                disposition = RevisionImpactItem.Disposition.LOCKED_CORRECTION
                note = "处罚已锁定：保留原合同/承包商快照，追加归属更正版本并重新复核"
                result.locked_count += 1
            else:
                disposition = RevisionImpactItem.Disposition.UNLOCKED_ATTRIBUTION
                note = "事件/处罚未锁定：确认发布后改挂归属"
                result.unlocked_count += 1
            to_contract_id, to_name, to_grid_id = resolved.id, resolved.contractor_name, resolved.grid_id

        result.items.append({
            "kind": "event",
            "event_id": event.id,
            "penalty_id": getattr(event, "penalty", None) and event.penalty.id,
            "disposition": disposition,
            "from_contract_id": from_contract_id,
            "from_contractor_name": from_name,
            "from_grid_id": from_grid_id,
            "to_contract_id": to_contract_id,
            "to_contractor_name": to_name,
            "to_grid_id": to_grid_id,
            "note": note,
        })

    # ---- 受影响照片（信息项，照片本身不可变）----
    seen_photos = set()
    for photo in _candidate_photos(proposal):
        if photo.id in seen_photos:
            continue
        seen_photos.add(photo.id)
        result.photo_count += 1
        result.items.append({
            "kind": "photo",
            "photo_id": photo.id,
            "event_id": photo.event_id,
            "disposition": RevisionImpactItem.Disposition.PHOTO_ONLY,
            "from_grid_id": photo.event.grid_id if photo.event_id else None,
            "to_grid_id": proposal.grid_id if proposal.target_type == RevisionProposal.Target.GRID else None,
            "note": "证据照片不可改；仅标记其归属随事件/网格修订而变化",
        })

    return result


def _rebuild_impact_items(proposal: RevisionProposal, result: ImpactResult) -> None:
    """根据计算结果重建待确认调整（pending）；旧 pending 项整体替换，不残留半套。"""
    proposal.impact_items.all().delete()
    rows = []
    for item in result.items:
        if item["kind"] == "photo":
            rows.append(RevisionImpactItem(
                proposal=proposal,
                photo_id=item["photo_id"],
                event_id=item.get("event_id"),
                disposition=item["disposition"],
                state=RevisionImpactItem.State.PENDING,
                from_grid_id=item.get("from_grid_id"),
                to_grid_id=item.get("to_grid_id"),
                note=item["note"],
            ))
        else:
            rows.append(RevisionImpactItem(
                proposal=proposal,
                event_id=item["event_id"],
                penalty_id=item.get("penalty_id"),
                disposition=item["disposition"],
                state=RevisionImpactItem.State.PENDING,
                from_contract_id=item["from_contract_id"],
                from_contractor_name=item["from_contractor_name"],
                from_grid_id=item.get("from_grid_id"),
                to_contract_id=item["to_contract_id"],
                to_contractor_name=item["to_contractor_name"],
                to_grid_id=item.get("to_grid_id"),
                note=item["note"],
            ))
    if rows:
        RevisionImpactItem.objects.bulk_create(rows, batch_size=500)


# --------------------------------------------------------------------------- #
# 创建提案
# --------------------------------------------------------------------------- #

@transaction.atomic
def create_contract_revision(
    *,
    contract: CleaningContract,
    new_contractor_name: str,
    new_valid_from: datetime,
    new_valid_to: datetime,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    new_grid: RoadGrid | None = None,
    reason: str,
    actor: str = "system",
    idempotency_key: str | None = None,
    clock: Clock | None = None,
) -> RevisionProposal:
    """为已有合同创建追溯/未来修订提案（承包商改派、责任区间更正、责任区迁移）。"""
    _assert_valid_range(new_valid_from, new_valid_to, label="合同责任区间")
    _assert_valid_range(effective_from, effective_to, label="修订有效时间")
    _check_idempotency(idempotency_key)

    contract = CleaningContract.objects.select_for_update().get(pk=contract.pk)
    _assert_no_draft(RevisionProposal.Target.CONTRACT, contract_id=contract.pk)

    proposal = RevisionProposal.objects.create(
        target_type=RevisionProposal.Target.CONTRACT,
        status=RevisionProposal.Status.DRAFT,
        grid=new_grid or contract.grid,
        contract=contract,
        new_grid=new_grid,
        effective_from=effective_from,
        effective_to=effective_to,
        new_contractor_name=new_contractor_name,
        new_valid_from=new_valid_from,
        new_valid_to=new_valid_to,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key or None,
        baseline_grid_geom=contract.grid.geom,
        baseline_grid_effective_from=contract.grid.effective_from,
        baseline_grid_effective_to=contract.grid.effective_to,
        baseline_contractor_name=contract.contractor_name,
        baseline_valid_from=contract.valid_from,
        baseline_valid_to=contract.valid_to,
        baseline_grid_id=contract.grid_id,
    )
    _finalize_proposal(proposal, clock=clock)
    return proposal


@transaction.atomic
def create_grid_revision(
    *,
    grid: RoadGrid,
    new_geom,
    new_name: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    reason: str,
    actor: str = "system",
    idempotency_key: str | None = None,
    clock: Clock | None = None,
) -> RevisionProposal:
    """为已有网格创建边界/名称修订提案（边界登记错误追溯更正）。"""
    _assert_valid_range(effective_from, effective_to, label="网格有效时间")
    _check_idempotency(idempotency_key)

    grid = RoadGrid.objects.select_for_update().get(pk=grid.pk)
    _assert_no_draft(RevisionProposal.Target.GRID, grid_id=grid.pk)

    proposal = RevisionProposal.objects.create(
        target_type=RevisionProposal.Target.GRID,
        status=RevisionProposal.Status.DRAFT,
        grid=grid,
        effective_from=effective_from,
        effective_to=effective_to,
        new_name=new_name if new_name is not None else grid.name,
        new_geom=new_geom,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key or None,
        baseline_grid_geom=grid.geom,
        baseline_grid_effective_from=grid.effective_from,
        baseline_grid_effective_to=grid.effective_to,
    )
    _finalize_proposal(proposal, clock=clock)
    return proposal


def _assert_no_draft(target_type, *, contract_id=None, grid_id=None) -> None:
    qs = RevisionProposal.objects.filter(target_type=target_type,
                                         status=RevisionProposal.Status.DRAFT)
    if contract_id is not None and qs.filter(contract_id=contract_id).exists():
        raise RevisionConflict("该合同已有待确认修订提案；请先撤回/替代/发布后再提交")
    if grid_id is not None and qs.filter(grid_id=grid_id, contract__isnull=True).exists():
        raise RevisionConflict("该网格已有待确认边界修订提案；请先撤回/替代/发布后再提交")


def _finalize_proposal(proposal: RevisionProposal, *, clock: Clock | None = None) -> None:
    result = compute_impact(proposal, clock=clock)
    _rebuild_impact_items(proposal, result)
    if result.blocked:
        # 创建阶段即暴露硬冲突：提案保留为 draft 并带拒绝原因，但不允许确认
        proposal.reject_reason = "；".join(result.conflicts) or (
            f"{result.unresolved_count} 个事件在修订后无法归属"
        )
        proposal.save(update_fields=["reject_reason", "updated_at"])


# --------------------------------------------------------------------------- #
# 预览（无状态）与刷新
# --------------------------------------------------------------------------- #

def preview_contract_revision(
    *, contract, new_contractor_name, new_valid_from, new_valid_to,
    effective_from=None, effective_to=None, new_grid=None,
    clock: Clock | None = None,
) -> ImpactResult:
    """不落库的合同修订影响预览。"""
    proposal = RevisionProposal(
        target_type=RevisionProposal.Target.CONTRACT,
        grid=new_grid or contract.grid, contract=contract, new_grid=new_grid,
        effective_from=effective_from, effective_to=effective_to,
        new_contractor_name=new_contractor_name,
        new_valid_from=new_valid_from, new_valid_to=new_valid_to,
    )
    # id=None 的临时对象仅用于纯计算
    proposal.id = None
    return compute_impact(proposal, clock=clock)


def preview_grid_revision(
    *, grid, new_geom, new_name=None, effective_from=None, effective_to=None,
    clock: Clock | None = None,
) -> ImpactResult:
    """不落库的网格边界修订影响预览。"""
    proposal = RevisionProposal(
        target_type=RevisionProposal.Target.GRID, grid=grid,
        effective_from=effective_from, effective_to=effective_to,
        new_name=new_name or grid.name, new_geom=new_geom,
    )
    proposal.id = None
    return compute_impact(proposal, clock=clock)


@transaction.atomic
def refresh_proposal(proposal: RevisionProposal, *, clock: Clock | None = None) -> ImpactResult:
    """
    基准漂移后的刷新恢复：以当前值重新锚定基准快照、重新计算影响并替换待确认调整；
    若仍有硬冲突，提案保持 draft 且不可确认（刷新不改变归属）。
    """
    proposal = RevisionProposal.objects.select_for_update().get(pk=proposal.pk)
    _assert_draft(proposal)

    # 重新锚定基准快照（刷新即“接受当前数据为新基准”）
    if proposal.target_type == RevisionProposal.Target.CONTRACT:
        contract = proposal.contract
        base_grid = contract.grid
        proposal.baseline_contractor_name = contract.contractor_name
        proposal.baseline_valid_from = contract.valid_from
        proposal.baseline_valid_to = contract.valid_to
        proposal.baseline_grid_id = contract.grid_id
        proposal.baseline_grid_geom = base_grid.geom
        proposal.baseline_grid_effective_from = base_grid.effective_from
        proposal.baseline_grid_effective_to = base_grid.effective_to
    else:
        grid = proposal.grid
        proposal.baseline_grid_geom = grid.geom
        proposal.baseline_grid_effective_from = grid.effective_from
        proposal.baseline_grid_effective_to = grid.effective_to
    proposal.save()

    result = compute_impact(proposal, clock=clock)
    _rebuild_impact_items(proposal, result)
    proposal.reject_reason = (
        "；".join(result.conflicts) or
        (f"{result.unresolved_count} 个事件在修订后无法归属" if result.unresolved_count else "")
    )
    proposal.save(update_fields=["reject_reason", "updated_at"])
    return result


# --------------------------------------------------------------------------- #
# 确认发布
# --------------------------------------------------------------------------- #

def _assert_draft(proposal: RevisionProposal) -> None:
    if proposal.status != RevisionProposal.Status.DRAFT:
        raise RevisionNotDraft(
            f"提案 {proposal.proposal_no} 当前状态 {proposal.status}，仅待确认提案可操作"
        )


def _assert_baseline_current(proposal: RevisionProposal) -> None:
    """乐观锁：发布时基准快照必须与当前值一致，否则要求先刷新。"""
    if proposal.target_type == RevisionProposal.Target.CONTRACT:
        contract = proposal.contract
        grid = proposal.new_grid or contract.grid
        stale = (
            contract.contractor_name != proposal.baseline_contractor_name
            or contract.valid_from != proposal.baseline_valid_from
            or contract.valid_to != proposal.baseline_valid_to
            or contract.grid_id != proposal.baseline_grid_id
            or grid.effective_from != proposal.baseline_grid_effective_from
            or grid.effective_to != proposal.baseline_grid_effective_to
        )
    else:
        grid = proposal.grid
        stale = (
            grid.effective_from != proposal.baseline_grid_effective_from
            or grid.effective_to != proposal.baseline_grid_effective_to
            or not _geom_equal(grid.geom, proposal.baseline_grid_geom)
        )
    if stale:
        raise RevisionStale("基准快照与当前数据不一致（可能已被其他修订改动），请先刷新预览")


def _geom_equal(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return a.equals(b)


@transaction.atomic
def confirm_proposal(proposal: RevisionProposal, *, actor: str = "system",
                     clock: Clock | None = None) -> RevisionProposal:
    """
    确认发布：行锁 + 重新冲突校验 + 基准乐观锁；全部命中后单事务应用。
    任何一步失败整体回滚，不产生半套数据（不发布修订、不留半应用影响项）。
    """
    clock = clock or SystemClock()
    proposal = RevisionProposal.objects.select_for_update().get(pk=proposal.pk)
    _assert_draft(proposal)

    # 重新计算（防止创建后数据变化），阻断硬冲突
    result = compute_impact(proposal, clock=clock)
    if result.conflicts:
        raise RevisionConflict("；".join(result.conflicts))
    if result.unresolved_count:
        raise RevisionUnresolvedImpact(
            f"{result.unresolved_count} 个事件在修订后无法归属（断档/重叠），请调整提案"
        )
    _assert_baseline_current(proposal)

    # 锁住所有受影响事件/处罚行，串行化并发发布
    impact_qs = proposal.impact_items.select_related("event", "penalty").order_by("id")
    event_ids = [it.event_id for it in impact_qs if it.event_id and
                 it.disposition == RevisionImpactItem.Disposition.UNLOCKED_ATTRIBUTION]
    penalty_ids = [it.penalty_id for it in impact_qs if it.penalty_id and
                   it.disposition == RevisionImpactItem.Disposition.LOCKED_CORRECTION]
    if event_ids:
        list(ProblemEvent.objects.select_for_update().filter(id__in=event_ids).order_by("id"))
    if penalty_ids:
        list(PenaltyUnit.objects.select_for_update().filter(id__in=penalty_ids).order_by("id"))

    published_at = clock.now()

    if proposal.target_type == RevisionProposal.Target.CONTRACT:
        _apply_contract_revision(proposal, impact_qs, actor=actor, published_at=published_at)
    else:
        _apply_grid_revision(proposal, impact_qs, actor=actor, published_at=published_at)

    proposal.status = RevisionProposal.Status.PUBLISHED
    proposal.published_at = published_at
    proposal.published_by = actor
    proposal.reject_reason = ""
    proposal.save(update_fields=[
        "status", "published_at", "published_by", "reject_reason", "updated_at",
    ])
    return proposal


def _apply_contract_revision(proposal, impact_qs, *, actor, published_at) -> None:
    contract = CleaningContract.objects.select_for_update().get(pk=proposal.contract_id)
    target_grid = proposal.new_grid or proposal.grid

    # 1) 发布合同新值 + 追加历史快照（旧值仍在历史链中，不就地删除）
    contract.grid = target_grid
    contract.contractor_name = proposal.new_contractor_name
    contract.valid_from = proposal.new_valid_from
    contract.valid_to = proposal.new_valid_to
    contract.save(update_fields=["grid", "contractor_name", "valid_from", "valid_to", "updated_at"])
    ContractHistory.objects.create(
        contract=contract, grid=target_grid, code=contract.code,
        contractor_name=contract.contractor_name,
        valid_from=contract.valid_from, valid_to=contract.valid_to,
        revision=proposal,
    )

    # 2) 逐项应用待确认调整
    for item in impact_qs:
        if item.disposition == RevisionImpactItem.Disposition.UNLOCKED_ATTRIBUTION:
            event = item.event
            event.grid_id = item.to_grid_id or target_grid.id
            event.contract_id = item.to_contract_id
            event.contractor_name = item.to_contractor_name
            event.save(update_fields=["grid", "contract", "contractor_name", "updated_at"])
            penalty = item.penalty
            if penalty is not None:
                penalty.contract_id = item.to_contract_id
                penalty.contractor_name = item.to_contractor_name
                penalty.save(update_fields=["contract", "contractor_name", "updated_at"])
            item.state = RevisionImpactItem.State.APPLIED

        elif item.disposition == RevisionImpactItem.Disposition.LOCKED_CORRECTION:
            _append_locked_correction(proposal, item, actor=actor, published_at=published_at)
            item.state = RevisionImpactItem.State.APPLIED

        elif item.disposition in (RevisionImpactItem.Disposition.FUTURE,
                                  RevisionImpactItem.Disposition.PHOTO_ONLY):
            # 未来事件历史不动；照片为信息项
            item.state = RevisionImpactItem.State.SKIPPED \
                if item.disposition == RevisionImpactItem.Disposition.FUTURE \
                else RevisionImpactItem.State.APPLIED

        item.save(update_fields=["state"])


def _apply_grid_revision(proposal, impact_qs, *, actor, published_at) -> None:
    grid = RoadGrid.objects.select_for_update().get(pk=proposal.grid_id)
    grid.geom = proposal.new_geom
    grid.name = proposal.new_name
    if proposal.effective_from is not None or proposal.effective_to is not None:
        grid.effective_from = proposal.effective_from
        grid.effective_to = proposal.effective_to
    grid.save()
    GridHistory.objects.create(
        grid=grid, code=grid.code, name=grid.name, geom=grid.geom,
        effective_from=grid.effective_from, effective_to=grid.effective_to,
        revision=proposal,
    )

    for item in impact_qs:
        if item.disposition == RevisionImpactItem.Disposition.UNLOCKED_ATTRIBUTION:
            event = item.event
            event.grid_id = item.to_grid_id
            event.contract_id = item.to_contract_id
            event.contractor_name = item.to_contractor_name
            event.save(update_fields=["grid", "contract", "contractor_name", "updated_at"])
            penalty = item.penalty
            if penalty is not None:
                penalty.contract_id = item.to_contract_id
                penalty.contractor_name = item.to_contractor_name
                penalty.save(update_fields=["contract", "contractor_name", "updated_at"])
            item.state = RevisionImpactItem.State.APPLIED

        elif item.disposition == RevisionImpactItem.Disposition.LOCKED_CORRECTION:
            _append_locked_correction(proposal, item, actor=actor, published_at=published_at)
            item.state = RevisionImpactItem.State.APPLIED

        elif item.disposition == RevisionImpactItem.Disposition.FUTURE:
            item.state = RevisionImpactItem.State.SKIPPED
        elif item.disposition == RevisionImpactItem.Disposition.PHOTO_ONLY:
            item.state = RevisionImpactItem.State.APPLIED
        item.save(update_fields=["state"])


def _append_locked_correction(proposal, item, *, actor, published_at) -> None:
    """
    已锁定处罚：保留原合同/承包商快照与锁定版本，
    追加 attribution 版本（进入待复核）+ AttributionCorrection 审计链。
    """
    penalty = PenaltyUnit.objects.select_for_update().get(pk=item.penalty_id)
    original_contract_id = penalty.contract_id
    original_name = penalty.contractor_name
    reason = (
        f"追溯归属更正（提案 {proposal.proposal_no}）：事件 {item.event.event_no} "
        f"原归属 {original_name}，按发生时责任区间应归 {item.to_contractor_name}；"
        f"原锁定快照保留，本追加版本待复核。原因：{proposal.reason}"
    )
    version = append_attribution_version(penalty, reason=reason, actor=actor)
    AttributionCorrection.objects.create(
        proposal=proposal,
        impact_item=item,
        penalty=penalty,
        version=version,
        from_contract_id=original_contract_id,
        from_contractor_name=original_name,
        to_contract_id=item.to_contract_id,
        to_contractor_name=item.to_contractor_name,
        actor=actor,
    )


# --------------------------------------------------------------------------- #
# 撤回 / 替代
# --------------------------------------------------------------------------- #

@transaction.atomic
def withdraw_proposal(proposal: RevisionProposal, *, actor: str = "system",
                      clock: Clock | None = None) -> RevisionProposal:
    """撤回待确认提案；待确认调整随提案作废（不影响任何归属）。"""
    clock = clock or SystemClock()
    proposal = RevisionProposal.objects.select_for_update().get(pk=proposal.pk)
    _assert_draft(proposal)
    proposal.status = RevisionProposal.Status.WITHDRAWN
    proposal.withdrawn_at = clock.now()
    proposal.save(update_fields=["status", "withdrawn_at", "updated_at"])
    return proposal


@transaction.atomic
def replace_proposal(old: RevisionProposal, *, updates: dict, actor: str = "system",
                     clock: Clock | None = None) -> RevisionProposal:
    """
    用新内容替代未发布提案：旧提案 superseded（保留其影响项作历史），
    新提案继承目标与基准、携带新内容并重新计算影响（单事务，无半套数据）。
    """
    clock = clock or SystemClock()
    old = RevisionProposal.objects.select_for_update().get(pk=old.pk)
    _assert_draft(old)

    # 先把旧提案移出 draft（满足同目标至多一个 draft 的约束），再建新提案
    old.status = RevisionProposal.Status.SUPERSEDED
    old.save(update_fields=["status", "updated_at"])

    new = RevisionProposal(
        target_type=old.target_type,
        grid=old.grid,
        contract=old.contract,
        status=RevisionProposal.Status.DRAFT,
        reason=updates.get("reason", old.reason),
        actor=actor,
        idempotency_key=updates.get("idempotency_key") or None,
        superseded_by=None,
    )
    # 复制可修订字段（新值优先，否则沿用旧提案）
    if old.target_type == RevisionProposal.Target.CONTRACT:
        new.new_grid = updates.get("new_grid", old.new_grid)
        new.grid = new.new_grid or old.grid
        new.new_contractor_name = updates.get("new_contractor_name", old.new_contractor_name)
        new.new_valid_from = updates.get("new_valid_from", old.new_valid_from)
        new.new_valid_to = updates.get("new_valid_to", old.new_valid_to)
        new.baseline_grid_id = old.baseline_grid_id
        new.baseline_contractor_name = old.baseline_contractor_name
        new.baseline_valid_from = old.baseline_valid_from
        new.baseline_valid_to = old.baseline_valid_to
    else:
        new.new_name = updates.get("new_name", old.new_name)
        new.new_geom = updates.get("new_geom", old.new_geom)
    new.effective_from = updates.get("effective_from", old.effective_from)
    new.effective_to = updates.get("effective_to", old.effective_to)
    new.baseline_grid_geom = old.baseline_grid_geom
    new.baseline_grid_effective_from = old.baseline_grid_effective_from
    new.baseline_grid_effective_to = old.baseline_grid_effective_to

    _check_idempotency(new.idempotency_key)
    new.save()

    old.superseded_by = new
    old.save(update_fields=["superseded_by", "updated_at"])

    _finalize_proposal(new, clock=clock)
    return new
