from django.contrib.gis import admin

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

admin.site.register(RoadGrid, admin.GISModelAdmin)
admin.site.register([CleaningContract, EvidencePhoto, DuplicateCandidate])
admin.site.register([ProblemEvent, Rectification])
admin.site.register([PenaltyUnit, PenaltyVersion, EscalationRecord, ReviewRecord])
admin.site.register([ResponsibilityRevision, RevisionImpactItem])
