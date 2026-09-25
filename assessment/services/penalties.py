"""
处罚单元与处罚版本服务。

规则：
* 版本只追加、永不修改（append-only）；
* 复核通过 -> 锁定当前版本（PenaltyUnit.locked_version）；
* 更正 / 逾期升级即便发生在锁定后，也只追加新版本，锁定行不变；
  新产生的版本重新进入“待复核”，历史扣分明细仍可完整追溯。
"""
from decimal import Decimal

from django.db import transaction

from assessment.models import PenaltyUnit, PenaltyVersion, ProblemEvent, ReviewRecord

# 各问题类别的基础扣分
BASE_POINTS = {
    ProblemEvent.Category.LITTER: Decimal("2.0"),
    ProblemEvent.Category.OVERFLOW: Decimal("3.0"),
    ProblemEvent.Category.ROAD_DIRT: Decimal("2.0"),
    ProblemEvent.Category.DEAD_CORNER: Decimal("4.0"),
    ProblemEvent.Category.OTHER: Decimal("1.0"),
}
ESCALATION_STEP = Decimal("1.0")
MAX_ESCALATION_LEVEL = 5


def base_points_for(category: str) -> Decimal:
    return BASE_POINTS.get(category, BASE_POINTS[ProblemEvent.Category.OTHER])


@transaction.atomic
def create_penalty_for_event(event: ProblemEvent, *, actor: str = "system") -> PenaltyUnit:
    """事件立案时创建处罚单元 + v1 初版扣分。"""
    points = base_points_for(event.category)
    penalty = PenaltyUnit.objects.create(
        event=event,
        contract=event.contract,
        contractor_name=event.contractor_name,
        points=points,
        escalation_level=0,
        status=PenaltyUnit.Status.DRAFT,
    )
    PenaltyVersion.objects.create(
        penalty=penalty,
        version_no=1,
        points=points,
        escalation_level=0,
        kind=PenaltyVersion.Kind.INITIAL,
        reason=f"事件 {event.event_no} 立案初版扣分（{event.get_category_display()}）",
        actor=actor,
    )
    return penalty


def _next_version_no(penalty: PenaltyUnit) -> int:
    last = penalty.versions.order_by("-version_no").first()
    return (last.version_no + 1) if last else 1


def append_version(
    penalty: PenaltyUnit,
    *,
    points: Decimal,
    escalation_level: int,
    kind: str,
    reason: str,
    actor: str,
) -> PenaltyVersion:
    version = PenaltyVersion.objects.create(
        penalty=penalty,
        version_no=_next_version_no(penalty),
        points=points,
        escalation_level=escalation_level,
        kind=kind,
        reason=reason,
        actor=actor,
    )
    penalty.points = points
    penalty.escalation_level = escalation_level
    # 追加版本后需要重新复核；已锁定版本指针保持不变
    if penalty.status == PenaltyUnit.Status.LOCKED:
        penalty.status = PenaltyUnit.Status.DRAFT
    penalty.save(update_fields=["points", "escalation_level", "status", "updated_at"])
    return version


@transaction.atomic
def correct_penalty(
    penalty: PenaltyUnit, *, points: Decimal, reason: str, actor: str
) -> PenaltyVersion:
    """人工更正：只允许追加版本，任何历史行都不改写。"""
    return append_version(
        penalty,
        points=points,
        escalation_level=penalty.escalation_level,
        kind=PenaltyVersion.Kind.CORRECTION,
        reason=reason,
        actor=actor,
    )


@transaction.atomic
def append_attribution_version(
    penalty: PenaltyUnit,
    *,
    reason: str,
    actor: str,
) -> PenaltyVersion:
    """
    追溯归属更正（命中已锁定处罚）：
    扣分与逾期等级不变，只把归属变更作为新版本追加（kind=attribution），
    处罚回到待复核；PenaltyUnit.contract / contractor_name 与 locked_version
    都保持为历史快照，绝不改写。
    """
    return append_version(
        penalty,
        points=penalty.points,
        escalation_level=penalty.escalation_level,
        kind=PenaltyVersion.Kind.ATTRIBUTION,
        reason=reason,
        actor=actor,
    )


@transaction.atomic
def escalate_penalty(
    penalty: PenaltyUnit, *, level: int, reason: str, actor: str
) -> PenaltyVersion:
    """逾期升级：以 v1 基础扣分为基数，每级加扣 ESCALATION_STEP。"""
    initial = penalty.versions.order_by("version_no").first()
    points = initial.points + ESCALATION_STEP * level
    return append_version(
        penalty,
        points=points,
        escalation_level=level,
        kind=PenaltyVersion.Kind.ESCALATION,
        reason=reason,
        actor=actor,
    )


@transaction.atomic
def review_penalty(
    penalty: PenaltyUnit, *, approved: bool, actor: str, comment: str = ""
) -> ReviewRecord:
    """
    复核：
    * approved=True  -> 锁定当前最新版本；
    * approved=False -> 记录驳回意见，处罚保持待复核（更正后可再次复核）。
    """
    current = penalty.versions.order_by("-version_no").first()
    if approved and penalty.status == PenaltyUnit.Status.LOCKED and penalty.locked_version_id == current.id:
        from assessment.exceptions import AlreadyReviewed

        raise AlreadyReviewed()

    record = ReviewRecord.objects.create(
        penalty=penalty,
        version=current,
        approved=approved,
        comment=comment,
        reviewer=actor,
    )
    if approved:
        penalty.status = PenaltyUnit.Status.LOCKED
        penalty.locked_version = current
        penalty.save(update_fields=["status", "locked_version", "updated_at"])
    return record
