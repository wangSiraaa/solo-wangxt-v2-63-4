"""DRF 序列化器。"""
from django.contrib.gis.geos import Point
from django.db import models
from rest_framework import serializers
from rest_framework_gis.fields import GeometryField
from drf_spectacular.utils import extend_schema_field

from assessment.models import (
    AttributionCorrection,
    CleaningContract,
    ContractHistory,
    DuplicateCandidate,
    EscalationRecord,
    EvidencePhoto,
    GridHistory,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    ReviewRecord,
    RevisionImpactItem,
    RevisionProposal,
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


class EventBriefSerializer(serializers.ModelSerializer):
    location = GeometryField(read_only=True)

    class Meta:
        model = ProblemEvent
        fields = [
            "id", "event_no", "category", "status", "location",
            "occurred_at", "grid", "contractor_name",
        ]


class AttributionCorrectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributionCorrection
        fields = [
            "id", "proposal", "impact_item", "penalty", "version",
            "from_contract", "from_contractor_name", "to_contract", "to_contractor_name",
            "actor", "created_at",
        ]
        read_only_fields = fields


class PenaltyUnitSerializer(serializers.ModelSerializer):
    event = EventBriefSerializer(read_only=True)
    versions = PenaltyVersionSerializer(many=True, read_only=True)
    escalations = EscalationRecordSerializer(many=True, read_only=True)
    reviews = ReviewRecordSerializer(many=True, read_only=True)
    attribution_corrections = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    locked_version_no = serializers.SerializerMethodField()

    class Meta:
        model = PenaltyUnit
        fields = [
            "id", "penalty_no", "event", "contract", "contractor_name",
            "points", "escalation_level", "status", "status_display",
            "locked_version", "locked_version_no",
            "versions", "escalations", "reviews", "attribution_corrections", "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_locked_version_no(self, obj) -> int | None:
        return obj.locked_version.version_no if obj.locked_version_id else None

    @extend_schema_field(AttributionCorrectionSerializer(many=True))
    def get_attribution_corrections(self, obj):
        # 追溯链：命中锁定处罚的归属更正（保留原快照 + 追加版本的因果链）
        return AttributionCorrectionSerializer(
            obj.attribution_corrections.select_related("version", "proposal"), many=True
        ).data


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


# =========================================================================== #
# 修订子系统
# =========================================================================== #

class RoadGridHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = GridHistory
        fields = [
            "id", "grid", "code", "name", "geom",
            "effective_from", "effective_to", "revision", "published_at",
        ]
        read_only_fields = fields


class ContractHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ContractHistory
        fields = [
            "id", "contract", "grid", "code", "contractor_name",
            "valid_from", "valid_to", "revision", "published_at",
        ]
        read_only_fields = fields


class RevisionImpactItemSerializer(serializers.ModelSerializer):
    disposition_display = serializers.CharField(source="get_disposition_display", read_only=True)
    state_display = serializers.CharField(source="get_state_display", read_only=True)

    class Meta:
        model = RevisionImpactItem
        fields = [
            "id", "proposal", "event", "photo", "penalty",
            "disposition", "disposition_display", "state", "state_display",
            "from_contract", "from_contractor_name", "to_contract", "to_contractor_name",
            "from_grid", "to_grid", "note",
        ]
        read_only_fields = fields


class RevisionProposalSerializer(serializers.ModelSerializer):
    """修订提案读取（含基准快照、影响项、归属更正审计链）。"""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    target_type_display = serializers.CharField(source="get_target_type_display", read_only=True)
    impact_items = RevisionImpactItemSerializer(many=True, read_only=True)
    attribution_corrections = AttributionCorrectionSerializer(many=True, read_only=True)
    impact_summary = serializers.SerializerMethodField()

    class Meta:
        model = RevisionProposal
        fields = [
            "id", "proposal_no", "status", "status_display",
            "target_type", "target_type_display",
            "grid", "contract", "new_grid",
            "effective_from", "effective_to",
            "new_contractor_name", "new_valid_from", "new_valid_to",
            "new_name", "new_geom",
            "baseline_grid_geom", "baseline_grid_effective_from", "baseline_grid_effective_to",
            "baseline_contractor_name", "baseline_valid_from", "baseline_valid_to",
            "baseline_grid_id",
            "reason", "actor", "idempotency_key", "reject_reason",
            "published_at", "published_by", "withdrawn_at", "superseded_by",
            "created_at", "updated_at",
            "impact_summary", "impact_items", "attribution_corrections",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField)
    def get_impact_summary(self, obj) -> dict:
        counts = {}
        for item in obj.impact_items.all():
            key = item.disposition
            counts.setdefault(key, {"total": 0, "pending": 0, "applied": 0, "skipped": 0})
            counts[key]["total"] += 1
            counts[key][item.state] = counts[key].get(item.state, 0) + 1
        return counts


class _RevisionActorMixin(serializers.Serializer):
    actor = serializers.CharField(required=False, default="system", max_length=64)
    reason = serializers.CharField(max_length=512)
    effective_from = serializers.DateTimeField(required=False, allow_null=True)
    effective_to = serializers.DateTimeField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(
        required=False, allow_null=True, allow_blank=False, max_length=64,
    )
    now = serializers.DateTimeField(required=False, allow_null=True, help_text="可选：注入当前时间")


class ContractRevisionCreateSerializer(_RevisionActorMixin):
    """合同修订提案（追溯改派/责任区间更正/责任区迁移）。"""

    new_contractor_name = serializers.CharField(max_length=128)
    new_valid_from = serializers.DateTimeField()
    new_valid_to = serializers.DateTimeField()
    new_grid = serializers.PrimaryKeyRelatedField(
        queryset=RoadGrid.objects.all(), required=False, allow_null=True,
    )


class GridRevisionCreateSerializer(_RevisionActorMixin):
    """网格边界/名称修订提案。"""

    new_geom = GeometryField(required=False)
    new_name = serializers.CharField(required=False, allow_blank=True, max_length=128)


class ContractRevisionPreviewSerializer(ContractRevisionCreateSerializer):
    reason = serializers.CharField(required=False, default="preview", max_length=512)


class GridRevisionPreviewSerializer(GridRevisionCreateSerializer):
    reason = serializers.CharField(required=False, default="preview", max_length=512)


class RevisionActionSerializer(serializers.Serializer):
    actor = serializers.CharField(required=False, default="system", max_length=64)
    now = serializers.DateTimeField(required=False, allow_null=True, help_text="可选：注入当前时间")


class ContractRevisionReplaceSerializer(serializers.Serializer):
    actor = serializers.CharField(required=False, default="system", max_length=64)
    now = serializers.DateTimeField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(required=False, allow_null=True, max_length=64)
    reason = serializers.CharField(required=False, max_length=512)
    new_contractor_name = serializers.CharField(required=False, max_length=128)
    new_valid_from = serializers.DateTimeField(required=False)
    new_valid_to = serializers.DateTimeField(required=False)
    new_grid = serializers.PrimaryKeyRelatedField(
        queryset=RoadGrid.objects.all(), required=False, allow_null=True,
    )
    effective_from = serializers.DateTimeField(required=False, allow_null=True)
    effective_to = serializers.DateTimeField(required=False, allow_null=True)


class GridRevisionReplaceSerializer(serializers.Serializer):
    actor = serializers.CharField(required=False, default="system", max_length=64)
    now = serializers.DateTimeField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(required=False, allow_null=True, max_length=64)
    reason = serializers.CharField(required=False, max_length=512)
    new_geom = GeometryField(required=False)
    new_name = serializers.CharField(required=False, allow_blank=True, max_length=128)
    effective_from = serializers.DateTimeField(required=False, allow_null=True)
    effective_to = serializers.DateTimeField(required=False, allow_null=True)


class ImpactReportSerializer(serializers.Serializer):
    """影响预览输出（无状态预览与提案刷新共用）。"""

    blocked = serializers.BooleanField()
    conflicts = serializers.ListField(child=serializers.CharField())
    counts = serializers.DictField()
    items = serializers.ListField(child=serializers.DictField())
