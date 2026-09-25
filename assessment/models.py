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

ResponsibilityRevision（责任区/合同修订提案：基准快照 + 有效时间，提案→预览→确认/撤回/替代）
                              │
              RevisionImpactItem（影响明细：确认前是待确认调整，确认后是审计链记录）
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
    """道路网格（PostGIS 多边形，SRID 4326）。"""

    code = models.CharField("网格编号", max_length=32, unique=True)
    name = models.CharField("网格名称", max_length=128, blank=True)
    geom = gis.PolygonField("网格范围", srid=4326)

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
        REVISION = "revision", "归属修订"

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


class ResponsibilityRevision(TimeStamped):
    """
    责任区/合同修订提案。

    生命周期：pending(待确认) -> published(已发布) / withdrawn(已撤回) / superseded(已被替代)。
    * 提案只登记“基准快照 + 修订内容 + 有效时间”，确认发布前不改任何归属；
    * 确认发布在单个事务内完成冲突复查、登记变更与影响应用，失败整体回滚；
    * 同一时空区间不得发布两个责任归属（合同区间重叠 / 网格面重叠一律拒绝）。
    """

    class TargetKind(models.TextChoices):
        GRID_BOUNDARY = "grid_boundary", "责任区边界修订"
        CONTRACT_INTERVAL = "contract_interval", "合同责任区间修订"

    class Status(models.TextChoices):
        PENDING = "pending", "待确认"
        PUBLISHED = "published", "已发布"
        WITHDRAWN = "withdrawn", "已撤回"
        SUPERSEDED = "superseded", "已被替代"

    revision_no = models.CharField("修订编号", max_length=32, unique=True, editable=False)
    target_kind = models.CharField("修订对象类型", max_length=20, choices=TargetKind.choices)
    grid = models.ForeignKey(
        RoadGrid, on_delete=models.PROTECT, related_name="revisions", verbose_name="目标网格",
    )
    target_contract = models.ForeignKey(
        CleaningContract, on_delete=models.PROTECT, related_name="revisions",
        null=True, blank=True, verbose_name="被修订合同（为空表示新增合同）",
    )
    baseline_snapshot = models.JSONField("基准快照（修订前登记状态）", default=dict)
    proposed_payload = models.JSONField("修订内容（新边界/新合同责任）", default=dict)
    effective_from = models.DateTimeField("生效时间（含）")
    effective_to = models.DateTimeField("生效结束时间（不含）", null=True, blank=True)
    reason = models.CharField("修订原因", max_length=512, blank=True)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.PENDING)
    # 提案幂等键：重复提交被拒绝（409）
    idempotency_key = models.CharField("幂等键", max_length=64, null=True, blank=True, unique=True)
    impact_summary = models.JSONField("影响汇总", default=dict)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="superseded_revisions",
        null=True, blank=True, verbose_name="本修订替代的旧修订",
    )
    created_by = models.CharField("提案人", max_length=64, blank=True, default="")
    previewed_at = models.DateTimeField("最近预览时间", null=True, blank=True)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    published_by = models.CharField("发布人", max_length=64, blank=True, default="")
    withdrawn_at = models.DateTimeField("撤回时间", null=True, blank=True)
    withdrawn_by = models.CharField("撤回人", max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "责任区/合同修订"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "target_kind"]),
            models.Index(fields=["grid", "effective_from", "effective_to"]),
        ]

    def save(self, *args, **kwargs):
        if not self.revision_no:
            self.revision_no = _new_id("RV")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.revision_no


class RevisionImpactItem(models.Model):
    """
    修订影响明细——精确到受影响的事件 / 处罚单元 / 证据照片。
    确认发布前是“待确认调整”（applied=False，不改归属）；
    确认发布后成为不可改写的审计链记录（applied=True）。
    """

    class ItemKind(models.TextChoices):
        EVENT = "event", "问题事件"
        PENALTY = "penalty", "处罚单元"
        PHOTO = "photo", "证据照片"

    class Disposition(models.TextChoices):
        REASSIGN_PENDING = "reassign_pending", "待确认调整（确认后改归属）"
        LOCKED_AUDIT = "locked_audit", "已锁定保留快照（追加审计链）"
        PHOTO_CONTEXT = "photo_context", "证据照片归属上下文变化"
        UNRESOLVED = "unresolved", "修订后无法归属（阻断确认）"

    revision = models.ForeignKey(
        ResponsibilityRevision, on_delete=models.CASCADE, related_name="impact_items", verbose_name="修订",
    )
    item_kind = models.CharField("对象类型", max_length=16, choices=ItemKind.choices)
    disposition = models.CharField("处置方式", max_length=20, choices=Disposition.choices)
    event = models.ForeignKey(
        ProblemEvent, on_delete=models.SET_NULL, related_name="revision_impacts",
        null=True, blank=True, verbose_name="受影响事件",
    )
    penalty = models.ForeignKey(
        PenaltyUnit, on_delete=models.SET_NULL, related_name="revision_impacts",
        null=True, blank=True, verbose_name="受影响处罚单元",
    )
    photo = models.ForeignKey(
        EvidencePhoto, on_delete=models.SET_NULL, related_name="revision_impacts",
        null=True, blank=True, verbose_name="受影响照片",
    )
    old_grid = models.ForeignKey(
        RoadGrid, on_delete=models.SET_NULL, related_name="+", null=True, blank=True, verbose_name="原网格",
    )
    new_grid = models.ForeignKey(
        RoadGrid, on_delete=models.SET_NULL, related_name="+", null=True, blank=True, verbose_name="新网格",
    )
    old_contract = models.ForeignKey(
        CleaningContract, on_delete=models.SET_NULL, related_name="+", null=True, blank=True, verbose_name="原合同",
    )
    new_contract = models.ForeignKey(
        CleaningContract, on_delete=models.SET_NULL, related_name="+", null=True, blank=True, verbose_name="新合同",
    )
    old_contract_code = models.CharField("原合同编号快照", max_length=32, blank=True, default="")
    new_contract_code = models.CharField("新合同编号快照", max_length=32, blank=True, default="")
    old_contractor_name = models.CharField("原承包商快照", max_length=128, blank=True, default="")
    new_contractor_name = models.CharField("新承包商快照", max_length=128, blank=True, default="")
    applied = models.BooleanField("是否已随发布生效", default=False)
    applied_at = models.DateTimeField("生效时间", null=True, blank=True)
    note = models.CharField("说明", max_length=512, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "修订影响明细"
        verbose_name_plural = verbose_name
        ordering = ["revision_id", "id"]
        indexes = [
            models.Index(fields=["revision", "item_kind"]),
            models.Index(fields=["penalty"]),
            models.Index(fields=["event"]),
        ]
