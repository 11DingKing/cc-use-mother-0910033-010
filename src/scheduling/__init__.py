"""主诊医师授权排班领域服务。"""
from .eligibility import (
    CoverageReport,
    PhysicianEvaluation,
    Rejection,
    evaluate_coverage,
    evaluate_physician,
)
from .errors import (
    CapacityError,
    ConflictError,
    CoverageError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from .models import (
    Assignment,
    AssignmentStatus,
    Authorization,
    AuthorizationStatus,
    AvailabilitySlot,
    Institution,
    Physician,
    Project,
    QualificationSnapshot,
    Shift,
    ShiftStatus,
    WorkLimitPolicy,
)
from .services import BookingService, RegistryService, SchedulingService
from .store import Store

__all__ = [
    "Assignment",
    "AssignmentStatus",
    "Authorization",
    "AuthorizationStatus",
    "AvailabilitySlot",
    "BookingService",
    "CapacityError",
    "ConflictError",
    "CoverageError",
    "CoverageReport",
    "DomainError",
    "Institution",
    "NotFoundError",
    "Physician",
    "PhysicianEvaluation",
    "Project",
    "QualificationSnapshot",
    "RegistryService",
    "Rejection",
    "SchedulingService",
    "Shift",
    "ShiftStatus",
    "Store",
    "ValidationError",
    "WorkLimitPolicy",
    "evaluate_coverage",
    "evaluate_physician",
]
