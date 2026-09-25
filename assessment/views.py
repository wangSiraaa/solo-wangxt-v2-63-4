"""API 视图。所有写操作都委托给 services 层（领域规则集中、时钟可注入）。"""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

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
    RevisionImpactItem,
    RevisionProposal,
    RoadGrid,
)
from assessment.serializers import (
    AttributionCorrectionSerializer,
    CandidateDecisionSerializer,
    CleaningContractSerializer,
    ContractHistorySerializer,
    ContractRevisionCreateSerializer,
    ContractRevisionPreviewSerializer,
    ContractRevisionReplaceSerializer,
    CreateEventFromPhotoSerializer,
    DuplicateCandidateSerializer,
    EscalationRecordSerializer,
    EscalationRunSerializer,
    EvidencePhotoSerializer,
    GridRevisionCreateSerializer,
    GridRevisionPreviewSerializer,
    GridRevisionReplaceSerializer,
    PenaltyCorrectionSerializer,
    PenaltyReviewSerializer,
    PenaltyUnitSerializer,
    PenaltyVersionSerializer,
    ProblemEventSerializer,
    RectificationReadSerializer,
    RectifyRequestSerializer,
    RevisionActionSerializer,
    RevisionImpactItemSerializer,
    RevisionProposalSerializer,
    RoadGridHistorySerializer,
    RoadGridSerializer,
)
from assessment.services.clock import resolve_clock
from assessment.services.decisions import decide_candidate
from assessment.services.escalation import run_escalation
from assessment.services.events import create_event_from_photo
from assessment.services.penalties import correct_penalty, review_penalty
from assessment.services import revisions
from assessment.services.rectification import submit_rectification


class RoadGridViewSet(viewsets.ModelViewSet):
    """道路网格（GeoJSON Feature 输入/输出）。"""

    queryset = RoadGrid.objects.all()
    serializer_class = RoadGridSerializer


class CleaningContractViewSet(viewsets.ModelViewSet):
    """保洁合同责任区间。"""

    queryset = CleaningContract.objects.select_related("grid").all()
    serializer_class = CleaningContractSerializer
    filterset_fields = ["grid", "contractor_name"]


class EvidencePhotoViewSet(viewsets.mixins.CreateModelMixin,
                          viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """
    证据照片：仅允许上传 / 查询，不允许修改删除（证据保全）。
    上传成功后响应中可查看自动生成的 pHash 与疑似候选（候选在 /candidates/）。
    """

    queryset = EvidencePhoto.objects.all()
    serializer_class = EvidencePhotoSerializer

    @action(detail=True, methods=["post"])
    def create_event(self, request, pk=None):
        """对一张尚未立案的照片直接创建事件（无候选时的入口）。"""
        photo = self.get_object()
        payload = CreateEventFromPhotoSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        event = create_event_from_photo(
            photo,
            category=data.get("category", ProblemEvent.Category.OTHER),
            description=data.get("description", ""),
            actor=data.get("actor", "system"),
            dedup_key=data.get("dedup_key"),
            occurred_at=data.get("occurred_at"),
        )
        return Response(ProblemEventSerializer(event).data, status=status.HTTP_201_CREATED)


class DuplicateCandidateViewSet(viewsets.mixins.RetrieveModelMixin,
                                viewsets.mixins.ListModelMixin,
                                viewsets.GenericViewSet):
    """疑似重复候选：只读列表 + 人工判定动作。"""

    queryset = DuplicateCandidate.objects.select_related("photo", "matched_photo").all()
    serializer_class = DuplicateCandidateSerializer
    filterset_fields = ["status", "photo", "matched_photo"]

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """
        人工判定：
        attach=同一问题不同角度（挂接、不重复扣分）；
        create_new=另立新事件（已整改事件复发/不同地点）；
        different=标记不同不合并。
        """
        candidate = self.get_object()
        payload = CandidateDecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        decided = decide_candidate(
            candidate,
            action=data["action"],
            actor=data.get("actor", "system"),
            category=data.get("category"),
            description=data.get("description", ""),
            dedup_key=data.get("dedup_key"),
            note=data.get("note", ""),
        )
        return Response(DuplicateCandidateSerializer(decided).data)


class ProblemEventViewSet(viewsets.mixins.RetrieveModelMixin,
                          viewsets.mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """问题事件：只读 + 整改回调。"""

    queryset = ProblemEvent.objects.select_related(
        "grid", "contract", "penalty", "rectification", "primary_photo",
    ).prefetch_related("photos").all()
    serializer_class = ProblemEventSerializer
    filterset_fields = ["status", "category", "grid", "contract", "contractor_name"]

    @action(detail=True, methods=["post"])
    def rectify(self, request, pk=None):
        """
        整改回调。重复回调返回 409（幂等忽略，不改变扣分）。
        可传 now 注入整改时间（测试/补录场景）。
        """
        event = self.get_object()
        payload = RectifyRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        clock = resolve_clock(data.get("now"))
        rectification = submit_rectification(
            event,
            note=data.get("note", ""),
            actor=data.get("actor", "system"),
            photo=data.get("photo_id"),
            clock=clock,
        )
        event.refresh_from_db()
        return Response(
            {
                "rectification": RectificationReadSerializer(rectification).data,
                "event": ProblemEventSerializer(event).data,
            },
            status=status.HTTP_201_CREATED,
        )


class RectificationViewSet(viewsets.mixins.RetrieveModelMixin,
                           viewsets.mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    queryset = Rectification.objects.select_related("event", "photo").all()
    serializer_class = RectificationReadSerializer
    filterset_fields = ["event"]


class PenaltyUnitViewSet(viewsets.mixins.RetrieveModelMixin,
                         viewsets.mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """
    处罚单元：只读检索（含完整版本链/证据/升级/复核记录，支撑扣分追溯），
    并提供“人工更正（追加版本）”与“复核（锁定版本）”两个动作。
    """

    queryset = PenaltyUnit.objects.select_related(
        "event", "contract", "locked_version",
    ).prefetch_related("versions", "escalations", "reviews").all()
    serializer_class = PenaltyUnitSerializer
    filterset_fields = ["status", "contractor_name", "contract", "event"]

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        """人工更正：追加一个 correction 版本，历史版本不动。"""
        penalty = self.get_object()
        payload = PenaltyCorrectionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        version = correct_penalty(
            penalty,
            points=data["points"],
            reason=data["reason"],
            actor=data.get("actor", "system"),
        )
        penalty.refresh_from_db()
        return Response(PenaltyUnitSerializer(penalty).data, status=status.HTTP_201_CREATED,
                        headers={"Version-No": str(version.version_no)})

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """复核：approved=true 时锁定当前最新版本。"""
        penalty = self.get_object()
        payload = PenaltyReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        review_penalty(
            penalty,
            approved=data["approved"],
            actor=data.get("actor", "reviewer"),
            comment=data.get("comment", ""),
        )
        penalty.refresh_from_db()
        return Response(PenaltyUnitSerializer(penalty).data)


class PenaltyVersionViewSet(viewsets.mixins.RetrieveModelMixin,
                            viewsets.mixins.ListModelMixin,
                            viewsets.GenericViewSet):
    """处罚版本（只读——版本只追加、不可改）。写方法一律 405。"""

    queryset = PenaltyVersion.objects.all()
    serializer_class = PenaltyVersionSerializer
    filterset_fields = ["penalty", "kind"]


class EscalationRecordViewSet(viewsets.mixins.RetrieveModelMixin,
                              viewsets.mixins.ListModelMixin,
                              viewsets.GenericViewSet):
    queryset = EscalationRecord.objects.select_related("penalty", "version").all()
    serializer_class = EscalationRecordSerializer
    filterset_fields = ["penalty", "level"]

    @action(detail=False, methods=["post"])
    def run(self, request):
        """
        执行一次逾期升级扫描。
        body 可传 {"now": "2026-09-25T10:00:00+08:00"} 注入时钟，
        不传则使用服务器当前时间。重复执行幂等。
        """
        payload = EscalationRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = run_escalation(
            clock=resolve_clock(data.get("now")),
            default_sla_hours=data.get("default_sla_hours"),
            actor=data.get("actor", "escalation-job"),
        )
        return Response(
            {
                "inspected_open_events": result.inspected,
                "overdue_open_events": result.open_overdue,
                "created_count": result.created_count,
                "created": EscalationRecordSerializer(result.created, many=True).data,
            },
            status=status.HTTP_201_CREATED if result.created_count else status.HTTP_200_OK,
        )


# --------------------------------------------------------------------------- #
# 修订子系统：历史快照、提案/预览/确认/撤回/替代、影响项、归属更正
# --------------------------------------------------------------------------- #

def _impact_report(result) -> dict:
    """把服务层 ImpactResult 转成 API 响应体。"""
    return {
        "blocked": result.blocked,
        "conflicts": result.conflicts,
        "counts": {
            "unlocked_attribution": result.unlocked_count,
            "locked_correction": result.locked_count,
            "unresolved": result.unresolved_count,
            "future": result.future_count,
            "photo": result.photo_count,
        },
        "items": result.items,
    }


class GridHistoryViewSet(viewsets.mixins.RetrieveModelMixin,
                         viewsets.mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """网格边界 append-only 历史快照（只读）。"""

    queryset = GridHistory.objects.all()
    serializer_class = RoadGridHistorySerializer
    filterset_fields = ["grid", "revision"]


class ContractHistoryViewSet(viewsets.mixins.RetrieveModelMixin,
                             viewsets.mixins.ListModelMixin,
                             viewsets.GenericViewSet):
    """合同责任区间 append-only 历史快照（只读）。"""

    queryset = ContractHistory.objects.all()
    serializer_class = ContractHistorySerializer
    filterset_fields = ["contract", "grid", "revision"]


class RevisionImpactItemViewSet(viewsets.mixins.RetrieveModelMixin,
                                viewsets.mixins.ListModelMixin,
                                viewsets.GenericViewSet):
    """修订影响项（待确认调整）只读检索。"""

    queryset = RevisionImpactItem.objects.select_related(
        "event", "photo", "penalty", "from_contract", "to_contract", "from_grid", "to_grid",
    ).all()
    serializer_class = RevisionImpactItemSerializer
    filterset_fields = ["proposal", "disposition", "state", "event", "penalty", "photo"]


class AttributionCorrectionViewSet(viewsets.mixins.RetrieveModelMixin,
                                   viewsets.mixins.ListModelMixin,
                                   viewsets.GenericViewSet):
    """命中锁定处罚的归属更正审计链（只读）。"""

    queryset = AttributionCorrection.objects.select_related(
        "proposal", "penalty", "version", "from_contract", "to_contract",
    ).all()
    serializer_class = AttributionCorrectionSerializer
    filterset_fields = ["proposal", "penalty"]


class RevisionProposalViewSet(viewsets.mixins.CreateModelMixin,
                              viewsets.mixins.RetrieveModelMixin,
                              viewsets.mixins.ListModelMixin,
                              viewsets.GenericViewSet):
    """
    责任区 / 合同修订提案。

    * POST /api/revisions/contracts/{contract_id}/propose/  合同修订提案
    * POST /api/revisions/grids/{grid_id}/propose/          网格边界修订提案
    * POST /api/revisions/contracts/{contract_id}/preview/  无状态合同影响预览
    * POST /api/revisions/grids/{grid_id}/preview/          无状态网格影响预览
    * POST /api/revisions/{id}/confirm/                     确认发布（单事务）
    * POST /api/revisions/{id}/withdraw/                    撤回
    * POST /api/revisions/{id}/replace/                     以新内容替代
    * POST /api/revisions/{id}/refresh/                     刷新影响（基准漂移恢复）
    """

    queryset = RevisionProposal.objects.prefetch_related(
        "impact_items", "attribution_corrections__version",
    ).select_related("grid", "contract", "new_grid").all()
    serializer_class = RevisionProposalSerializer
    filterset_fields = ["status", "target_type", "grid", "contract"]

    # -- 合同修订 -- #
    @action(detail=False, methods=["post"], url_path=r"contracts/(?P<contract_id>[0-9]+)/propose")
    def propose_contract(self, request, contract_id=None):
        contract = get_object_or_404(CleaningContract.objects.select_related("grid"), pk=contract_id)
        payload = ContractRevisionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        proposal = revisions.create_contract_revision(
            contract=contract,
            new_contractor_name=d["new_contractor_name"],
            new_valid_from=d["new_valid_from"],
            new_valid_to=d["new_valid_to"],
            effective_from=d.get("effective_from"),
            effective_to=d.get("effective_to"),
            new_grid=d.get("new_grid"),
            reason=d["reason"],
            actor=d.get("actor", "system"),
            idempotency_key=d.get("idempotency_key"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(RevisionProposalSerializer(proposal).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path=r"contracts/(?P<contract_id>[0-9]+)/preview")
    def preview_contract(self, request, contract_id=None):
        contract = get_object_or_404(CleaningContract.objects.select_related("grid"), pk=contract_id)
        payload = ContractRevisionPreviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        result = revisions.preview_contract_revision(
            contract=contract,
            new_contractor_name=d["new_contractor_name"],
            new_valid_from=d["new_valid_from"],
            new_valid_to=d["new_valid_to"],
            effective_from=d.get("effective_from"),
            effective_to=d.get("effective_to"),
            new_grid=d.get("new_grid"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(_impact_report(result))

    # -- 网格修订 -- #
    @action(detail=False, methods=["post"], url_path=r"grids/(?P<grid_id>[0-9]+)/propose")
    def propose_grid(self, request, grid_id=None):
        grid = get_object_or_404(RoadGrid, pk=grid_id)
        payload = GridRevisionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        new_geom = d.get("new_geom") or grid.geom
        proposal = revisions.create_grid_revision(
            grid=grid,
            new_geom=new_geom,
            new_name=d.get("new_name"),
            effective_from=d.get("effective_from"),
            effective_to=d.get("effective_to"),
            reason=d["reason"],
            actor=d.get("actor", "system"),
            idempotency_key=d.get("idempotency_key"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(RevisionProposalSerializer(proposal).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path=r"grids/(?P<grid_id>[0-9]+)/preview")
    def preview_grid(self, request, grid_id=None):
        grid = get_object_or_404(RoadGrid, pk=grid_id)
        payload = GridRevisionPreviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        result = revisions.preview_grid_revision(
            grid=grid,
            new_geom=d.get("new_geom") or grid.geom,
            new_name=d.get("new_name"),
            effective_from=d.get("effective_from"),
            effective_to=d.get("effective_to"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(_impact_report(result))

    # -- 提案动作 -- #
    def _proposal(self, pk):
        return RevisionProposal.objects.prefetch_related(
            "impact_items", "attribution_corrections__version",
        ).get(pk=pk)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """确认发布：重新冲突校验 + 基准乐观锁，全部命中后单事务应用。"""
        proposal = self.get_object()
        payload = RevisionActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        revisions.confirm_proposal(
            proposal, actor=d.get("actor", "system"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(RevisionProposalSerializer(self._proposal(pk)).data)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        """撤回待确认提案（待确认调整随提案作废，不改任何归属）。"""
        proposal = self.get_object()
        payload = RevisionActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        revisions.withdraw_proposal(
            proposal, actor=d.get("actor", "system"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(RevisionProposalSerializer(self._proposal(pk)).data)

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        """基准漂移后刷新影响（重新计算待确认调整，不改归属）。"""
        proposal = self.get_object()
        payload = RevisionActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        result = revisions.refresh_proposal(
            proposal, clock=resolve_clock(d.get("now")),
        )
        return Response({
            "proposal": RevisionProposalSerializer(self._proposal(pk)).data,
            "impact": _impact_report(result),
        })

    @action(detail=True, methods=["post"])
    def replace(self, request, pk=None):
        """以新内容替代未发布提案：旧提案 superseded，新提案重新计算影响。"""
        proposal_obj = self.get_object()
        serializer_cls = (
            ContractRevisionReplaceSerializer
            if proposal_obj.target_type == RevisionProposal.Target.CONTRACT
            else GridRevisionReplaceSerializer
        )
        payload = serializer_cls(data=request.data)
        payload.is_valid(raise_exception=True)
        d = payload.validated_data
        if proposal_obj.target_type == RevisionProposal.Target.CONTRACT:
            updates = {
                "new_contractor_name": d["new_contractor_name"],
                "new_valid_from": d["new_valid_from"],
                "new_valid_to": d["new_valid_to"],
                "new_grid": d.get("new_grid"),
                "effective_from": d.get("effective_from"),
                "effective_to": d.get("effective_to"),
                "reason": d["reason"],
                "idempotency_key": d.get("idempotency_key"),
            }
        else:
            updates = {
                "new_geom": d.get("new_geom") or proposal_obj.new_geom,
                "new_name": d.get("new_name") or proposal_obj.new_name,
                "effective_from": d.get("effective_from"),
                "effective_to": d.get("effective_to"),
                "reason": d["reason"],
                "idempotency_key": d.get("idempotency_key"),
            }
        new_proposal = revisions.replace_proposal(
            proposal_obj, updates=updates, actor=d.get("actor", "system"),
            clock=resolve_clock(d.get("now")),
        )
        return Response(RevisionProposalSerializer(self._proposal(new_proposal.id)).data,
                        status=status.HTTP_201_CREATED)
