from django.contrib.gis import admin

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

admin.site.register(RoadGrid, admin.GISModelAdmin)
admin.site.register([CleaningContract, EvidencePhoto, DuplicateCandidate])
admin.site.register([ProblemEvent, Rectification])
admin.site.register([PenaltyUnit, PenaltyVersion, EscalationRecord, ReviewRecord])
# 修订子系统
admin.site.register([GridHistory, ContractHistory], admin.GISModelAdmin)
admin.site.register(RevisionProposal, admin.GISModelAdmin)
admin.site.register([RevisionImpactItem, AttributionCorrection])
