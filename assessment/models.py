"""
考核领域模型：

道路网格 RoadGrid ──┐
                  ├──< CleaningContract（合同责任区间：网格 × 起止时间 × 承包商）
                  └──< ProblemEvent（问题事件：发生位置、发生时间）
                              │
                   EvidencePhoto（感知哈希 + 候选关联，可挂接到事件）
                              │
                     PenaltyUnit 1:1（唯一扣分单元，归属发生时的合同）
                              │
              PenaltyVersion（只追加、不可变）/ ReviewRecord / EscalationRecord
"""
import uuid

from django.contrib.gis.db import models as gis
from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class RoadGrid(gis.Model):
    """
    道路网格（PostGIS 多边形，SRID 4326）。

    effective_from/effective_to 为网格边界本身的有效时间（含起、不含止，
    NULL 表示负无穷/正无穷）。历史数据默认为 NULL，归属行为与旧版完全一致。
    网格边界登记错误时通过修订提案（RevisionProposal）更正，不直接覆盖历史。
    """

    code = models.CharField("网格编号", max_length=32, unique=True)
    name = models.CharField("网格名称", max_length=128, blank=True)
    geom = gis.PolygonField("网格范围", srid=4326)
    effective_from = models.DateTimeField("边界有效开始（含，NULL=最早）", null=True, blank=True)
    effective_to = models.DateTimeField("边界有效结束（不含，NULL=最晚）", null=True, blank=True)

    class Meta:
        verbose_name = "道路网格"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.name}".strip()


class CleaningContract(TimeStamped):
    """
    保洁合同责任区间：同一网格在同一时间只允许一个生效合同
    （业务上由归属服务校验重叠，录入端也应避免重叠）。
    扣分归属按“事件发生时刻落在哪个 [valid_from, valid_to) 区间”确定。
    """

    code = models.CharField("合同编号", max_length=32, unique=True)
    grid = gis.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="contracts", verbose_name="道路网格")
    contractor_name = models.CharField("承包商名称", max_length=128)
    valid_from = models.DateTimeField("责任开始时间（含）")
    valid_to = models.DateTimeField("责任结束时间（不含）")

    class Meta:
        verbose_name = "保洁合同"
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=["grid", "valid_from", "valid_to"]),
            models.Index(fields=["contractor_name"]),
        ]

    def __str__(self):
        return f"{self.code} {self.contractor_name}"


class ProblemEvent(TimeStamped):
    """
    现场问题事件。一个事件 = 一次扣分口径；不同角度照片挂同一事件，
    整改后复发必须新建事件。
    """

    class Category(models.TextChoices):
        LITTER = "litter", "散落垃圾"
        OVERFLOW = "overflow", "垃圾桶满溢"
        ROAD_DIRT = "road_dirt", "路面污渍"
        DEAD_CORNER = "dead_corner", "卫生死角"
        OTHER = "other", "其他问题"

    class Status(models.TextChoices):
        OPEN = "open", "待整改"
        RECTIFIED = "rectified", "已整改"

    event_no = models.CharField("事件编号", max_length=32, unique=True, editable=False)
    primary_photo = gis.ForeignKey(
        "EvidencePhoto",
        on_delete=models.PROTECT,
        related_name="primary_of",
        null=True,
        blank=True,
        verbose_name="首报照片",
    )
    grid = gis.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="events", null=True, verbose_name="所在网格")
    # 归属快照：按“发生时刻”解析出的合同；后续合同/承包商变更不改变历史归属
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="events",
        null=True, verbose_name="责任合同（按发生时解析）",
    )
    contractor_name = models.CharField("责任承包商快照", max_length=128, blank=True)
    category = models.CharField("问题类别", max_length=20, choices=Category.choices)
    description = models.CharField("问题描述", max_length=512, blank=True)
    location = gis.PointField("发生位置", srid=4326)
    occurred_at = models.DateTimeField("发生/拍摄时间", help_text="归属与 SLA 都以该时间为准，而不是录入时间")
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.OPEN)
    sla_hours = models.PositiveIntegerField("整改时限(小时)", default=24)
    # 外部业务幂等键，防止同一条立案请求重复提交
    dedup_key = models.CharField("外部去重键", max_length=64, null=True, blank=True, unique=True)

    class Meta:
        verbose_name = "问题事件"
        verbose_name_plural = verbose_name
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["status", "occurred_at"]),
            models.Index(fields=["contractor_name"]),
        ]

    def save(self, *args, **kwargs):
        if not self.event_no:
            self.event_no = _new_id("EV")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.event_no


class EvidencePhoto(TimeStamped):
    """
    证据照片。上传时由 Pillow 计算 256 位感知哈希(pHash)。
    pHash 只用于生成 DuplicateCandidate（疑似重复候选）；
    照片是否属于同一事件，最终由人工按位置/时间判断。
    照片一旦创建不可删除、不可篡改（证据保全）。
    """

    image = models.ImageField("图片文件", upload_to="evidence/%Y/%m/%d")
    phash = models.CharField("感知哈希(hex)", max_length=64, db_index=True, editable=False)
    captured_at = models.DateTimeField("拍摄时间")
    location = gis.PointField("拍摄位置", srid=4326)
    uploader = models.CharField("上传人", max_length=64, blank=True, default="")
    note = models.CharField("备注", max_length=256, blank=True)
    event = models.ForeignKey(
        ProblemEvent, on_delete=models.PROTECT, related_name="photos",
        null=True, blank=True, verbose_name="关联事件",
    )

    class Meta:
        verbose_name = "证据照片"
        verbose_name_plural = verbose_name
        ordering = ["-captured_at"]


class DuplicateCandidate(models.Model):
    """
    疑似重复候选（pHash 汉明距离 <= 阈值时生成）。
    只表达“图片相似”，不自动合并事件；最终状态由人工判定给出。
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待判定"
        CONFIRMED_DUPLICATE = "confirmed_duplicate", "确认同问题（挂接，不重复扣分）"
        RECURRENCE = "recurrence", "整改后复发（新建事件）"
        DIFFERENT = "different", "不同地点/不同问题（不合并）"
        REJECTED = "rejected", "误报忽略"

    photo = models.ForeignKey(EvidencePhoto, on_delete=models.CASCADE, related_name="candidates", verbose_name="新照片")
    matched_photo = models.ForeignKey(EvidencePhoto, on_delete=models.CASCADE, related_name="matched_as", verbose_name="相似照片")
    hamming_distance = models.PositiveSmallIntegerField("pHash汉明距离")
    status = models.CharField("判定状态", max_length=24, choices=Status.choices, default=Status.PENDING)
    decision_note = models.CharField("判定说明", max_length=256, blank=True)
    decided_by = models.CharField("判定人", max_length=64, blank=True)
    decided_at = models.DateTimeField("判定时间", null=True, blank=True)

    class Meta:
        verbose_name = "疑似重复候选"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["photo", "matched_photo"], name="uniq_candidate_pair"),
        ]
        indexes = [models.Index(fields=["status"])]
        ordering = ["-id"]

    def __str__(self):
        return f"{self.photo_id}~{self.matched_photo_id} d={self.hamming_distance} {self.status}"


class Rectification(models.Model):
    """整改记录（每个事件至多一条；重复回调返回 409 并被忽略）。"""

    event = models.OneToOneField(
        ProblemEvent, on_delete=models.PROTECT, related_name="rectification", verbose_name="问题事件",
    )
    photo = models.ForeignKey(
        EvidencePhoto, on_delete=models.PROTECT, related_name="rectifications",
        null=True, blank=True, verbose_name="整改后照片",
    )
    note = models.CharField("整改说明", max_length=512, blank=True)
    submitted_by = models.CharField("提交人", max_length=64, blank=True, default="")
    submitted_at = models.DateTimeField("整改提交时间")

    class Meta:
        verbose_name = "整改记录"
        verbose_name_plural = verbose_name


class PenaltyUnit(TimeStamped):
    """
    处罚单元（扣分的最小且唯一归属单元）：一个事件一个 PenaltyUnit。
    每笔扣分都能通过 penalty_no 追到：事件、责任合同/承包商、全部证据、全部版本。
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "待复核"
        LOCKED = "locked", "已锁定"

    penalty_no = models.CharField("处罚单号", max_length=32, unique=True, editable=False)
    event = models.OneToOneField(ProblemEvent, on_delete=models.PROTECT, related_name="penalty", verbose_name="问题事件")
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="penalties",
        null=True, verbose_name="责任合同快照",
    )
    contractor_name = models.CharField("责任承包商快照", max_length=128, blank=True)
    points = models.DecimalField("当前有效扣分", max_digits=6, decimal_places=1)
    escalation_level = models.PositiveSmallIntegerField("逾期升级等级", default=0)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.DRAFT)
    locked_version = models.ForeignKey(
        "PenaltyVersion", on_delete=models.PROTECT, related_name="locked_by_penalties",
        null=True, blank=True, verbose_name="最近锁定版本",
    )

    class Meta:
        verbose_name = "处罚单元"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.penalty_no:
            self.penalty_no = _new_id("PN")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.penalty_no


class PenaltyVersion(models.Model):
    """
    处罚版本——只追加(append-only)、不可变：
    初版立案、逾期升级、人工更正都只能新增一行；
    复核通过时由 PenaltyUnit.locked_version 指向锁定行，历史行永不修改。
    """

    class Kind(models.TextChoices):
        INITIAL = "initial", "初版立案"
        ESCALATION = "escalation", "逾期升级"
        CORRECTION = "correction", "人工更正"
        ATTRIBUTION = "attribution", "追溯归属更正"

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="versions", verbose_name="处罚单元")
    version_no = models.PositiveIntegerField("版本号")
    points = models.DecimalField("扣分", max_digits=6, decimal_places=1)
    escalation_level = models.PositiveSmallIntegerField("逾期等级", default=0)
    kind = models.CharField("版本类型", max_length=16, choices=Kind.choices)
    reason = models.CharField("原因", max_length=512)
    actor = models.CharField("操作人/任务", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "处罚版本"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["penalty", "version_no"], name="uniq_penalty_version_no"),
        ]
        ordering = ["penalty_id", "version_no"]


class EscalationRecord(models.Model):
    """逾期升级执行记录（可注入时钟，按 (处罚单, 等级) 幂等）。"""

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="escalations", verbose_name="处罚单元")
    level = models.PositiveSmallIntegerField("升级到等级")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT, related_name="escalations", verbose_name="产生的处罚版本")
    reason = models.CharField("升级原因", max_length=512)
    actor = models.CharField("执行任务", max_length=64, blank=True, default="")
    ran_at = models.DateTimeField("执行时间(注入时钟)")

    class Meta:
        verbose_name = "升级记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(fields=["penalty", "level"], name="uniq_escalation_level"),
        ]
        ordering = ["penalty_id", "level"]


class ReviewRecord(models.Model):
    """复核记录：复核通过即锁定当前版本。"""

    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT, related_name="reviews", verbose_name="处罚单元")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT, related_name="reviews", verbose_name="被复核版本")
    approved = models.BooleanField("是否通过")
    comment = models.CharField("复核意见", max_length=512, blank=True)
    reviewer = models.CharField("复核人", max_length=64, blank=True, default="")
    reviewed_at = models.DateTimeField("复核时间", auto_now_add=True)

    class Meta:
        verbose_name = "复核记录"
        verbose_name_plural = verbose_name
        ordering = ["-reviewed_at"]


# ============================================================================
# 责任区 / 合同修订子系统
#
# 关键不变量：
# * “按发生时归属”的历史不可直接覆盖；网格/合同每次发布都产生一行 append-only
#   历史快照（GridHistory / ContractHistory），不做就地删除；
# * 修订提案带“基准快照 + 有效时间”，提交即生成影响项（待确认调整），
#   确认发布之前，任何事件/处罚的归属都不改变；
# * 同一时空区间不得存在两个责任归属（网格不重叠；同网格合同区间不重叠）；
# * 命中已锁定处罚时，原合同/承包商快照与已锁定版本指针保持不变，
#   只追加 attribution 版本并进入待复核，同时写入 AttributionCorrection 审计链。
# ============================================================================


class GridHistory(models.Model):
    """道路网格边界的 append-only 历史快照（每次发布一行，永不修改）。"""

    grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="history", verbose_name="道路网格")
    code = models.CharField("网格编号快照", max_length=32)
    name = models.CharField("网格名称快照", max_length=128, blank=True)
    geom = gis.PolygonField("网格范围快照", srid=4326)
    effective_from = models.DateTimeField("边界有效开始（含，NULL=最早）", null=True, blank=True)
    effective_to = models.DateTimeField("边界有效结束（不含，NULL=最晚）", null=True, blank=True)
    revision = models.ForeignKey(
        "RevisionProposal", on_delete=models.PROTECT, related_name="grid_history_rows",
        null=True, blank=True, verbose_name="发布该快照的修订（NULL=迁移基线）",
    )
    published_at = models.DateTimeField("发布时间", auto_now_add=True)

    class Meta:
        verbose_name = "网格历史快照"
        verbose_name_plural = verbose_name
        ordering = ["grid_id", "-published_at", "-id"]
        indexes = [models.Index(fields=["grid", "effective_from", "effective_to"])]


class ContractHistory(models.Model):
    """保洁合同责任区间的 append-only 历史快照（每次发布一行，永不修改）。"""

    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="history", verbose_name="保洁合同",
    )
    grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="contract_history_rows",
                             verbose_name="网格快照")
    code = models.CharField("合同编号快照", max_length=32)
    contractor_name = models.CharField("承包商名称快照", max_length=128)
    valid_from = models.DateTimeField("责任开始时间快照（含）")
    valid_to = models.DateTimeField("责任结束时间快照（不含）")
    revision = models.ForeignKey(
        "RevisionProposal", on_delete=models.PROTECT, related_name="contract_history_rows",
        null=True, blank=True, verbose_name="发布该快照的修订（NULL=迁移基线）",
    )
    published_at = models.DateTimeField("发布时间", auto_now_add=True)

    class Meta:
        verbose_name = "合同历史快照"
        verbose_name_plural = verbose_name
        ordering = ["contract_id", "-published_at", "-id"]
        indexes = [models.Index(fields=["grid", "valid_from", "valid_to"])]


class RevisionProposal(TimeStamped):
    """
    责任区 / 合同修订提案。

    生命周期：draft →（confirm）→ published；draft 可 withdraw；
    任何未发布提案都可用 replace 以新内容替代（旧提案 superseded、新提案继承影响项）。
    提案携带基准快照（baseline_*）与目标内容、有效时间；发布时重新校验基准是否漂移
    （乐观锁）与时空重叠，全部命中才在单事务内应用。
    """

    class Target(models.TextChoices):
        GRID = "grid", "道路网格"
        CONTRACT = "contract", "保洁合同"

    class Status(models.TextChoices):
        DRAFT = "draft", "待确认"
        PUBLISHED = "published", "已发布"
        WITHDRAWN = "withdrawn", "已撤回"
        SUPERSEDED = "superseded", "已被替代"
        REJECTED = "rejected", "校验拒绝"

    proposal_no = models.CharField("提案编号", max_length=32, unique=True, editable=False)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    target_type = models.CharField("修订对象类型", max_length=16, choices=Target.choices)
    grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="revision_proposals",
                             verbose_name="目标/相关网格")
    contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="revision_proposals",
        null=True, blank=True, verbose_name="目标合同（合同修订）",
    )
    effective_from = models.DateTimeField("修订有效开始（含）", null=True, blank=True)
    effective_to = models.DateTimeField("修订有效结束（不含）", null=True, blank=True)

    reason = models.CharField("修订原因", max_length=512)
    actor = models.CharField("提案人", max_length=64, blank=True, default="")
    idempotency_key = models.CharField("幂等键", max_length=64, null=True, blank=True)

    # ---- 合同修订内容（target_type=contract）----
    new_contractor_name = models.CharField("修订后承包商", max_length=128, blank=True, default="")
    new_valid_from = models.DateTimeField("修订后责任开始（含）", null=True, blank=True)
    new_valid_to = models.DateTimeField("修订后责任结束（不含）", null=True, blank=True)
    new_grid = models.ForeignKey(
        RoadGrid, on_delete=models.PROTECT, related_name="incoming_contract_proposals",
        null=True, blank=True, verbose_name="改挂网格（合同责任区迁移）",
    )

    # ---- 网格修订内容（target_type=grid）----
    new_name = models.CharField("修订后网格名称", max_length=128, blank=True, default="")
    new_geom = gis.PolygonField("修订后网格范围", srid=4326, null=True, blank=True)

    # ---- 基准快照（乐观锁：发布时与当前值比对）----
    baseline_grid_geom = gis.PolygonField("基准网格范围", srid=4326, null=True, blank=True)
    baseline_grid_effective_from = models.DateTimeField("基准网格有效开始", null=True, blank=True)
    baseline_grid_effective_to = models.DateTimeField("基准网格有效结束", null=True, blank=True)
    baseline_contractor_name = models.CharField("基准承包商", max_length=128, blank=True, default="")
    baseline_valid_from = models.DateTimeField("基准合同开始", null=True, blank=True)
    baseline_valid_to = models.DateTimeField("基准合同结束", null=True, blank=True)
    baseline_grid_id = models.BigIntegerField("基准合同所属网格ID", null=True, blank=True)

    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    published_by = models.CharField("发布人", max_length=64, blank=True, default="")
    withdrawn_at = models.DateTimeField("撤回时间", null=True, blank=True)
    superseded_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="supersedes",
        null=True, blank=True, verbose_name="替代本提案的新提案",
    )
    reject_reason = models.CharField("拒绝原因", max_length=512, blank=True, default="")

    class Meta:
        verbose_name = "修订提案"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="uniq_revision_idempotency_key",
            ),
            # 同一目标只允许一个进行中（draft）提案，防止重复提交产生两套待确认调整
            models.UniqueConstraint(
                fields=["contract"],
                condition=models.Q(target_type="contract", status="draft"),
                name="uniq_draft_contract_revision",
            ),
            models.UniqueConstraint(
                fields=["grid"],
                condition=models.Q(target_type="grid", status="draft"),
                name="uniq_draft_grid_revision",
            ),
        ]
        indexes = [
            models.Index(fields=["target_type", "status"]),
            models.Index(fields=["grid", "status"]),
        ]

    def save(self, *args, **kwargs):
        if not self.proposal_no:
            self.proposal_no = _new_id("RP")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.proposal_no} {self.target_type} {self.status}"


class RevisionImpactItem(models.Model):
    """
    修订影响项 = “待确认调整”的载体：提案创建/刷新时计算并持久化，
    在提案发布之前，不改变任何事件/处罚的归属。

    发布时按 disposition 精确应用：
    * unlocked_attribution —— 改挂未锁定事件/处罚的归属（追加审计行）；
    * locked_correction    —— 已锁定处罚：原快照不动，追加 attribution 版本复核，
                             并生成 AttributionCorrection；
    * unresolved           —— 修订后区间内解析不到/有多个责任归属（断档或重叠），
                             属于硬冲突，提案不允许确认；
    * future               —— 事件发生在有效时间之后，修订只影响新事件，历史不动。
    """

    class Disposition(models.TextChoices):
        UNLOCKED_ATTRIBUTION = "unlocked_attribution", "未锁定：确认后改挂归属"
        LOCKED_CORRECTION = "locked_correction", "已锁定：追加更正/复核"
        UNRESOLVED = "unresolved", "无法解析（断档/重叠）"
        FUTURE = "future", "未来事件，本次不调整"
        PHOTO_ONLY = "photo_only", "仅关联照片（信息项）"

    class State(models.TextChoices):
        PENDING = "pending", "待确认"
        APPLIED = "applied", "已应用"
        SKIPPED = "skipped", "已跳过"

    proposal = models.ForeignKey(RevisionProposal, on_delete=models.PROTECT, related_name="impact_items",
                                 verbose_name="修订提案")
    event = models.ForeignKey(
        ProblemEvent, on_delete=models.PROTECT, related_name="revision_impacts",
        null=True, blank=True, verbose_name="受影响事件",
    )
    photo = models.ForeignKey(
        EvidencePhoto, on_delete=models.PROTECT, related_name="revision_impacts",
        null=True, blank=True, verbose_name="受影响照片",
    )
    penalty = models.ForeignKey(
        PenaltyUnit, on_delete=models.PROTECT, related_name="revision_impacts",
        null=True, blank=True, verbose_name="受影响处罚",
    )

    disposition = models.CharField("处置类型", max_length=24, choices=Disposition.choices)
    state = models.CharField("处理状态", max_length=16, choices=State.choices, default=State.PENDING)

    from_contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="impact_items_from",
        null=True, blank=True, verbose_name="修订前归属合同",
    )
    from_contractor_name = models.CharField("修订前承包商", max_length=128, blank=True, default="")
    to_contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="impact_items_to",
        null=True, blank=True, verbose_name="修订后归属合同",
    )
    to_contractor_name = models.CharField("修订后承包商", max_length=128, blank=True, default="")
    from_grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="impact_items_from_grid",
                                  null=True, blank=True, verbose_name="修订前网格")
    to_grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT, related_name="impact_items_to_grid",
                                null=True, blank=True, verbose_name="修订后网格")
    note = models.CharField("说明", max_length=512, blank=True, default="")

    class Meta:
        verbose_name = "修订影响项"
        verbose_name_plural = verbose_name
        ordering = ["id"]
        indexes = [
            models.Index(fields=["proposal", "disposition"]),
            models.Index(fields=["state"]),
        ]

    def __str__(self):
        return f"{self.proposal_id}:{self.disposition}:{self.state}"


class AttributionCorrection(models.Model):
    """
    命中已锁定处罚的归属更正审计链：
    原 PenaltyUnit.contract / contractor_name 与 locked_version 永不改变，
    只追加一个 attribution 版本（进入待复核），本行记录其与修订提案的因果链。
    """

    proposal = models.ForeignKey(RevisionProposal, on_delete=models.PROTECT,
                                 related_name="attribution_corrections", verbose_name="修订提案")
    impact_item = models.ForeignKey(RevisionImpactItem, on_delete=models.PROTECT,
                                    related_name="attribution_corrections", verbose_name="影响项")
    penalty = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT,
                                related_name="attribution_corrections", verbose_name="处罚单元")
    version = models.ForeignKey(PenaltyVersion, on_delete=models.PROTECT,
                                related_name="attribution_corrections", verbose_name="追加的归属版本")
    from_contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="attribution_corrections_from",
        null=True, blank=True, verbose_name="更正前合同（保留）",
    )
    from_contractor_name = models.CharField("更正前承包商（保留快照）", max_length=128)
    to_contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="attribution_corrections_to",
        null=True, blank=True, verbose_name="应归合同",
    )
    to_contractor_name = models.CharField("应归承包商", max_length=128)
    actor = models.CharField("操作人", max_length=64, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "归属更正审计"
        verbose_name_plural = verbose_name
        ordering = ["-id"]
