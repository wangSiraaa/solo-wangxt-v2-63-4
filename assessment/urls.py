from django.urls import include, path
from rest_framework.routers import DefaultRouter

from assessment import views

router = DefaultRouter()
router.register("grids", views.RoadGridViewSet, basename="grid")
router.register("contracts", views.CleaningContractViewSet, basename="contract")
router.register("photos", views.EvidencePhotoViewSet, basename="photo")
router.register("candidates", views.DuplicateCandidateViewSet, basename="candidate")
router.register("events", views.ProblemEventViewSet, basename="event")
router.register("rectifications", views.RectificationViewSet, basename="rectification")
router.register("penalties", views.PenaltyUnitViewSet, basename="penalty")
router.register("penalty-versions", views.PenaltyVersionViewSet, basename="penaltyversion")
router.register("escalations", views.EscalationRecordViewSet, basename="escalation")
# 修订子系统
router.register("revisions", views.RevisionProposalViewSet, basename="revision")
router.register("revision-impacts", views.RevisionImpactItemViewSet, basename="revisionimpact")
router.register("attribution-corrections", views.AttributionCorrectionViewSet,
                basename="attributioncorrection")
router.register("grid-history", views.GridHistoryViewSet, basename="gridhistory")
router.register("contract-history", views.ContractHistoryViewSet, basename="contracthistory")

urlpatterns = [
    path("", include(router.urls)),
]
