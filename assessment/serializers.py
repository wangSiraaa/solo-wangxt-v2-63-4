"""DRF 序列化器。"""
from django.contrib.gis.geos import Point
from django.db import models
from rest_framework import serializers
from rest_framework_gis.fields import GeometryField
from drf_spectacular.utils import extend_schema_field

from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    ResponsibilityRevision,
    ReviewRecord,
    RevisionImpactItem,
    RoadGrid,
)
from assessment.services.duplicates import generate_candidates_for_photo
from assessment.services.phash import compute_phash_hex


class RoadGridSerializer(serializers.ModelSerializer):
    """道路网格；geom 为 GeoJSON Polygon（EPSG:4326）。"""

    geom = GeometryField()

    class Meta:
        model = RoadGrid
        fields = ["id", "code", "name", "geom"]


class CleaningContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = CleaningContract
        fields = ["id", "code", "grid", "contractor_name", "valid_from", "valid_to", "created_at"]
        read_only_fields = ["created_at"]


class EvidencePhotoSerializer(serializers.ModelSerializer):
    # 上传时只给经纬度，服务端构造 Point；location 以 GeoJSON 只读返回
    lat = serializers.FloatField(write_only=True, min_value=-90, max_value=90)
    lng = serializers.FloatField(write_only=True, min_value=-180, max_value=180)
    location = GeometryField(read_only=True)

    class Meta:
        model = EvidencePhoto
        fields = [
            "id", "image", "phash", "captured_at", "lat", "lng",
            "location", "uploader", "note", "event", "created_at",
        ]
        read_only_fields = ["phash", "event", "created_at"]

    def create(self, validated_data):
        lat = validated_data.pop("lat")
        lng = validated_data.pop("lng")
        image = validated_data["image"]
        validated_data["phash"] = compute_phash_hex(image)
        image.seek(0)
        validated_data["location"] = Point(lng, lat, srid=4326)
        photo = EvidencePhoto.objects.create(**validated_data)
        # 仅依据 pHash 生成疑似重复候选；不做任何自动合并
        generate_candidates_for_photo(photo)
        return photo


class PhotoBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = EvidencePhoto
        fields = ["id", "phash", "captured_at", "location", "event", "note"]


class DuplicateCandidateSerializer(serializers.ModelSerializer):
    photo = PhotoBriefSerializer(read_only=True)
    matched_photo = PhotoBriefSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DuplicateCandidate
        fields = [
            "id", "photo", "matched_photo", "hamming_distance",
            "status", "status_display", "decision_note", "decided_by", "decided_at",
        ]
        read_only_fields = fields


class CandidateDecisionSerializer(serializers.Serializer):
    class Action(models.TextChoices):
        ATTACH = "attach", "确认同一问题，挂接到被匹配事件（不重复扣分）"
        CREATE_NEW = "create_new", "另立新事件（整改后复发 / 不同地点）"
        DIFFERENT = "different", "标记为不同，暂不处理"
        REJECTED = "rejected", "误报忽略"

    action = serializers.ChoiceField(choices=Action.choices)
    category = serializers.ChoiceField(choices=ProblemEvent.Category.choices, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    dedup_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="system", max_length=64)


class CreateEventFromPhotoSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=ProblemEvent.Category.choices, required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    dedup_key = serializers.CharField(required=False, allow_blank=False, max_length=64)
    occurred_at = serializers.DateTimeField(required=False)
    actor = serializers.CharField(required=False, default="system", max_length=64)


class RectificationReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rectification
        fields = ["id", "event", "photo", "note", "submitted_by", "submitted_at"]


class RectifyRequestSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="system", max_length=64)
    photo_id = serializers.PrimaryKeyRelatedField(
        queryset=EvidencePhoto.objects.all(), required=False, allow_null=True,
    )
    now = serializers.DateTimeField(required=False, help_text="可选：注入整改提交时间")


class PenaltyVersionSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = PenaltyVersion
        fields = [
            "id", "version_no", "points", "escalation_level",
            "kind", "kind_display", "reason", "actor", "created_at",
        ]
        read_only_fields = fields


class EscalationRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = EscalationRecord
        fields = ["id", "penalty", "level", "version", "reason", "actor", "ran_at"]
        read_only_fields = fields


class ReviewRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewRecord
        fields = ["id", "penalty", "version", "approved", "comment", "reviewer", "reviewed_at"]
        read_only_fields = fields


class RevisionImpactItemSerializer(serializers.ModelSerializer):
    """修订影响明细（确认前=待确认调整，确认后=审计链记录）。"""

    revision_no = serializers.CharField(source="revision.revision_no", read_only=True)
    item_kind_display = serializers.CharField(source="get_item_kind_display", read_only=True)
    disposition_display = serializers.CharField(source="get_disposition_display", read_only=True)

    class Meta:
        model = RevisionImpactItem
        fields = [
            "id", "revision", "revision_no", "item_kind", "item_kind_display",
            "disposition", "disposition_display",
            "event", "penalty", "photo",
            "old_grid", "new_grid", "old_contract", "new_contract",
            "old_contract_code", "new_contract_code",
            "old_contractor_name", "new_contractor_name",
            "applied", "applied_at", "note", "created_at",
        ]
        read_only_fields = fields


class EventBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "category", "status", "location",
            "occurred_at", "grid", "contractor_name",
        ]


class PenaltyUnitSerializer(serializers.ModelSerializer):
    event = EventBriefSerializer(read_only=True)
    versions = PenaltyVersionSerializer(many=True, read_only=True)
    escalations = EscalationRecordSerializer(many=True, read_only=True)
    reviews = ReviewRecordSerializer(many=True, read_only=True)
    # 处罚追溯：归属修订审计链（锁定处罚保留快照，修订只追加记录）
    revision_impacts = RevisionImpactItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    locked_version_no = serializers.SerializerMethodField()

    class Meta:
        model = PenaltyUnit
        fields = [
            "id", "penalty_no", "event", "contract", "contractor_name",
            "points", "escalation_level", "status", "status_display",
            "locked_version", "locked_version_no",
            "versions", "escalations", "reviews", "revision_impacts", "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_locked_version_no(self, obj) -> int | None:
        return obj.locked_version.version_no if obj.locked_version_id else None


class PenaltyCorrectionSerializer(serializers.Serializer):
    points = serializers.DecimalField(max_digits=6, decimal_places=1, min_value=0)
    reason = serializers.CharField(max_length=512)
    actor = serializers.CharField(required=False, default="system", max_length=64)


class PenaltyReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    actor = serializers.CharField(required=False, default="reviewer", max_length=64)


class EscalationRunSerializer(serializers.Serializer):
    now = serializers.DateTimeField(required=False, help_text="注入的当前时间；不传则用服务器时钟")
    default_sla_hours = serializers.IntegerField(required=False, min_value=1)
    actor = serializers.CharField(required=False, default="escalation-job", max_length=64)


class ProblemEventSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)
    photos = PhotoBriefSerializer(many=True, read_only=True)
    penalty = PenaltyUnitSerializer(read_only=True)
    rectification = RectificationReadSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "primary_photo", "grid", "contract", "contractor_name",
            "category", "category_display", "description", "location",
            "occurred_at", "status", "status_display", "sla_hours", "dedup_key",
            "photos", "penalty", "rectification", "created_at",
        ]
        read_only_fields = fields


class ResponsibilityRevisionSerializer(serializers.ModelSerializer):
    """修订提案（含基准快照、修订内容、有效时间与影响明细）。"""

    impact_items = RevisionImpactItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    target_kind_display = serializers.CharField(source="get_target_kind_display", read_only=True)
    superseded_by = serializers.SerializerMethodField()

    class Meta:
        model = ResponsibilityRevision
        fields = [
            "id", "revision_no", "target_kind", "target_kind_display",
            "grid", "target_contract", "baseline_snapshot", "proposed_payload",
            "effective_from", "effective_to", "reason",
            "status", "status_display", "idempotency_key", "impact_summary",
            "supersedes", "superseded_by",
            "created_by", "created_at", "previewed_at",
            "published_at", "published_by", "withdrawn_at", "withdrawn_by",
            "impact_items",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_superseded_by(self, obj) -> int | None:
        newer = obj.superseded_revisions.first()
        return newer.id if newer else None


class RevisionCreateSerializer(serializers.Serializer):
    """新建修订提案 / 替代修订的请求体。"""

    target_kind = serializers.ChoiceField(choices=ResponsibilityRevision.TargetKind.choices)
    grid = serializers.PrimaryKeyRelatedField(queryset=RoadGrid.objects.all())
    target_contract = serializers.PrimaryKeyRelatedField(
        queryset=CleaningContract.objects.all(), required=False, allow_null=True,
        help_text="被修订的合同；留空表示在网格内新增合同区间",
    )
    proposed_payload = serializers.JSONField(
        help_text='边界修订: {"geom": <GeoJSON Polygon>}；合同修订: {"contractor_name": "...", "code": "新增时必填"}',
    )
    effective_from = serializers.DateTimeField(help_text="生效时间（含）")
    effective_to = serializers.DateTimeField(
        required=False, allow_null=True, help_text="生效结束时间（不含）；合同修订必填",
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=512)
    idempotency_key = serializers.CharField(
        required=False, allow_blank=False, max_length=64,
        help_text="提案幂等键；重复提交返回 409",
    )
    actor = serializers.CharField(required=False, default="system", max_length=64)


class RevisionActionSerializer(serializers.Serializer):
    """预览/确认/撤回动作的请求体（可注入时钟）。"""

    actor = serializers.CharField(required=False, default="system", max_length=64)
    now = serializers.DateTimeField(required=False, help_text="可选：注入动作时间")
