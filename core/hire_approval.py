from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.agent_role_catalog import SPECIALIST_ROLE_ORDER
from core.project_team_state import ProjectSpecialistRoster

if TYPE_CHECKING:
    from core.project_registry import ProjectRegistry, ProjectSnapshot

_SPECIALIST_ORDER_INDEX = {
    role: index for index, role in enumerate(SPECIALIST_ROLE_ORDER)
}
_REQUEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_ALLOWED_REQUEST_SOURCES = frozenset(
    {"logical_hiring_pm_hint", "owner_command"}
)
_ALLOWED_REQUEST_STATUSES = frozenset({"pending", "approved", "rejected"})
_ALLOWED_DECISIONS = frozenset({"approve", "reject"})
_ALLOWED_RESULT_STATUSES = frozenset(
    {
        "pending_created",
        "pending_exists",
        "approved",
        "rejected",
        "already_applied",
        "blocked_by_policy",
        "not_found",
    }
)


def _normalize_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("empty_project_id")
    normalized = project_id.strip().lower()
    if not normalized.isascii():
        raise ValueError("non_ascii_project_id")
    if not normalized[0].isalpha():
        raise ValueError(f"invalid_project_id:{normalized}")
    for char in normalized:
        if not (char.islower() or char.isdigit() or char == "_"):
            raise ValueError(f"invalid_project_id:{normalized}")
    if len(normalized) > 64:
        raise ValueError(f"invalid_project_id:{normalized}")
    return normalized


def _normalize_specialist_role(role: str) -> str:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("empty_specialist_role")
    normalized = role.strip().lower()
    if normalized not in _SPECIALIST_ORDER_INDEX:
        raise ValueError(f"unknown_specialist_role:{normalized}")
    return normalized


def _normalize_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("empty_hire_request_reason")
    return reason.strip()


def _normalize_request_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("empty_hire_request_source")
    normalized = source.strip().lower()
    if normalized not in _ALLOWED_REQUEST_SOURCES:
        raise ValueError(f"invalid_hire_request_source:{normalized}")
    return normalized


def _normalize_request_status(status: str) -> str:
    if not isinstance(status, str) or not status.strip():
        raise ValueError("empty_hire_request_status")
    normalized = status.strip().lower()
    if normalized not in _ALLOWED_REQUEST_STATUSES:
        raise ValueError(f"invalid_hire_request_status:{normalized}")
    return normalized


def _normalize_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("empty_hire_request_id")
    normalized = request_id.strip().lower()
    if not normalized.isascii() or not _REQUEST_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_hire_request_id:{normalized}")
    return normalized


def _normalize_decision(decision: str) -> str:
    if not isinstance(decision, str) or not decision.strip():
        raise ValueError("empty_hire_approval_decision")
    normalized = decision.strip().lower()
    if normalized not in _ALLOWED_DECISIONS:
        raise ValueError(f"invalid_hire_approval_decision:{normalized}")
    return normalized


def _normalize_result_status(status: str) -> str:
    if not isinstance(status, str) or not status.strip():
        raise ValueError("empty_hire_approval_result_status")
    normalized = status.strip().lower()
    if normalized not in _ALLOWED_RESULT_STATUSES:
        raise ValueError(f"invalid_hire_approval_result_status:{normalized}")
    return normalized


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _normalize_timestamp(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return normalized


def generate_hire_request_id(
    *,
    clock: float | None = None,
) -> str:
    created_at = _normalize_timestamp(
        time.time() if clock is None else clock,
        field_name="created_at",
    )
    return _normalize_request_id(
        f"hire-{int(created_at * 1000)}-{uuid.uuid4().hex[:8]}"
    )


@dataclass(frozen=True)
class PendingHireRequest:
    request_id: str
    project_id: str
    specialist_role: str
    reason: str
    source: str
    status: str
    created_at: float
    decided_at: float | None = None
    decided_by_user_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _normalize_request_id(self.request_id),
        )
        object.__setattr__(
            self,
            "project_id",
            _normalize_project_id(self.project_id),
        )
        object.__setattr__(
            self,
            "specialist_role",
            _normalize_specialist_role(self.specialist_role),
        )
        object.__setattr__(
            self,
            "reason",
            _normalize_reason(self.reason),
        )
        object.__setattr__(
            self,
            "source",
            _normalize_request_source(self.source),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_request_status(self.status),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        if self.decided_at is not None:
            object.__setattr__(
                self,
                "decided_at",
                _normalize_timestamp(self.decided_at, field_name="decided_at"),
            )
        if self.decided_by_user_id is not None:
            object.__setattr__(
                self,
                "decided_by_user_id",
                _validate_positive_int(
                    self.decided_by_user_id,
                    field_name="hire_request_decided_by_user_id",
                ),
            )
        if self.status == "pending":
            if self.decided_at is not None or self.decided_by_user_id is not None:
                raise ValueError("pending_hire_request_cannot_be_decided")
            return
        if self.decided_at is None or self.decided_by_user_id is None:
            raise ValueError(
                "decided_hire_request_requires_decision_metadata"
            )


@dataclass(frozen=True)
class HireApprovalDecision:
    request_id: str
    decision: str
    actor_user_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _normalize_request_id(self.request_id),
        )
        object.__setattr__(
            self,
            "decision",
            _normalize_decision(self.decision),
        )
        object.__setattr__(
            self,
            "actor_user_id",
            _validate_positive_int(
                self.actor_user_id,
                field_name="hire_approval_actor_user_id",
            ),
        )


@dataclass(frozen=True)
class HireApprovalResult:
    project_id: str
    request_id: str | None
    status: str
    roster_before: ProjectSpecialistRoster
    roster_after: ProjectSpecialistRoster
    message_text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_project_id(self.project_id),
        )
        if self.request_id is not None:
            object.__setattr__(
                self,
                "request_id",
                _normalize_request_id(self.request_id),
            )
        object.__setattr__(
            self,
            "status",
            _normalize_result_status(self.status),
        )
        if not isinstance(self.roster_before, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_roster_before_type:"
                f"{type(self.roster_before).__name__}"
            )
        if not isinstance(self.roster_after, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_roster_after_type:"
                f"{type(self.roster_after).__name__}"
            )
        if self.roster_before.project_id != self.project_id:
            raise ValueError(
                "hire_approval_roster_before_project_id_mismatch:"
                f"{self.roster_before.project_id}!={self.project_id}"
            )
        if self.roster_after.project_id != self.project_id:
            raise ValueError(
                "hire_approval_roster_after_project_id_mismatch:"
                f"{self.roster_after.project_id}!={self.project_id}"
            )
        if not isinstance(self.message_text, str) or not self.message_text.strip():
            raise ValueError("empty_hire_approval_message_text")
        if self.status == "approved":
            if self.request_id is None:
                raise ValueError("approved_hire_result_requires_request_id")
            if self.roster_after == self.roster_before:
                raise ValueError(
                    "approved_hire_result_requires_roster_change"
                )
            if not set(self.roster_after.specialist_roles).issuperset(
                self.roster_before.specialist_roles
            ):
                raise ValueError(
                    "approved_hire_result_roster_after_must_extend_before"
                )
        else:
            if (
                self.status in {"pending_created", "pending_exists", "rejected"}
                and self.request_id is None
            ):
                raise ValueError(
                    "pending_or_rejected_hire_result_requires_request_id"
                )
            if self.roster_after != self.roster_before:
                raise ValueError(
                    "non_approved_hire_result_forbids_roster_change"
                )


class HireApprovalService:
    def __init__(self, project_registry: ProjectRegistry) -> None:
        from core.project_registry import ProjectRegistry as _ProjectRegistry

        if not isinstance(project_registry, _ProjectRegistry):
            raise ValueError(
                "invalid_project_registry_type:"
                f"{type(project_registry).__name__}"
            )
        self._project_registry = project_registry

    @property
    def project_registry(self) -> ProjectRegistry:
        return self._project_registry

    def get_hire_request(self, request_id: str) -> PendingHireRequest | None:
        return self._project_registry.get_hire_request(request_id)

    def list_pending_requests(
        self,
        project_id: str,
    ) -> tuple[PendingHireRequest, ...]:
        return self._project_registry.list_pending_hire_requests(project_id)

    def request_sensitive_hire(
        self,
        snapshot: ProjectSnapshot,
        specialist_role: str,
        reason: str,
        source: str,
        *,
        created_at: float | None = None,
    ) -> HireApprovalResult:
        self._validate_snapshot(snapshot)
        project_id = snapshot.project.project_id
        normalized_role = _normalize_specialist_role(specialist_role)
        normalized_reason = _normalize_reason(reason)
        normalized_source = _normalize_request_source(source)
        current_snapshot = self._project_registry.get_project_snapshot(project_id)
        if current_snapshot is None:
            raise ValueError(f"unknown_project_id:{project_id}")
        roster_before = self._project_registry.get_project_specialist_roster(project_id)
        if (
            current_snapshot.policy is None
            or not current_snapshot.policy.allow_hiring
        ):
            return HireApprovalResult(
                project_id=project_id,
                request_id=None,
                status="blocked_by_policy",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Sensitive logical hire заблокирован policy проекта: "
                    "persisted project roster пока не изменён."
                ),
            )
        if not current_snapshot.policy.require_owner_approval_for_hires:
            raise ValueError("hire_owner_approval_not_required_for_project")
        if roster_before.contains(normalized_role):
            return HireApprovalResult(
                project_id=project_id,
                request_id=None,
                status="already_applied",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Pending approval не требуется: specialist уже есть в "
                    "persisted project roster."
                ),
            )
        request = PendingHireRequest(
            request_id=generate_hire_request_id(clock=created_at),
            project_id=project_id,
            specialist_role=normalized_role,
            reason=normalized_reason,
            source=normalized_source,
            status="pending",
            created_at=(
                time.time() if created_at is None else float(created_at)
            ),
        )
        try:
            persisted_request = self._project_registry.create_pending_hire_request(
                request
            )
        except ValueError as exc:
            if str(exc) == (
                "project_specialist_already_present:"
                f"{project_id}:{normalized_role}"
            ):
                current_roster = self._project_registry.get_project_specialist_roster(
                    project_id
                )
                return HireApprovalResult(
                    project_id=project_id,
                    request_id=None,
                    status="already_applied",
                    roster_before=current_roster,
                    roster_after=current_roster,
                    message_text=(
                        "🧩 Pending approval не требуется: specialist уже есть в "
                        "persisted project roster."
                    ),
                )
            raise
        if persisted_request.status != "pending":
            raise ValueError(
                "created_hire_request_must_be_pending:"
                f"{persisted_request.status}"
            )
        if persisted_request.request_id == request.request_id:
            status = "pending_created"
            message_text = (
                "🧩 Sensitive logical hire ждёт owner approval: request "
                f"`{persisted_request.request_id}` создан для "
                f"`{persisted_request.specialist_role}`. Persisted project "
                "roster пока не изменён."
            )
        else:
            status = "pending_exists"
            message_text = (
                "🧩 Sensitive logical hire уже ждёт owner approval: request "
                f"`{persisted_request.request_id}` для "
                f"`{persisted_request.specialist_role}` остаётся pending. "
                "Persisted project roster пока не изменён."
            )
        return HireApprovalResult(
            project_id=project_id,
            request_id=persisted_request.request_id,
            status=status,
            roster_before=roster_before,
            roster_after=roster_before,
            message_text=message_text,
        )

    def apply_decision(
        self,
        snapshot: ProjectSnapshot,
        decision: HireApprovalDecision,
    ) -> HireApprovalResult:
        self._validate_snapshot(snapshot)
        if not isinstance(decision, HireApprovalDecision):
            raise ValueError(
                "invalid_hire_approval_decision_type:"
                f"{type(decision).__name__}"
            )
        project_id = snapshot.project.project_id
        current_snapshot = self._project_registry.get_project_snapshot(project_id)
        if current_snapshot is None:
            raise ValueError(f"unknown_project_id:{project_id}")
        if decision.actor_user_id != current_snapshot.project.owner_user_id:
            raise ValueError("hire_approval_requires_owner")
        roster_before = self._project_registry.get_project_specialist_roster(project_id)
        request = self._project_registry.get_hire_request(decision.request_id)
        if request is None or request.project_id != project_id:
            return HireApprovalResult(
                project_id=project_id,
                request_id=decision.request_id,
                status="not_found",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Pending hire request не найден для текущего проекта."
                ),
            )
        if decision.decision == "reject":
            if request.status == "approved":
                return HireApprovalResult(
                    project_id=project_id,
                    request_id=request.request_id,
                    status="already_applied",
                    roster_before=roster_before,
                    roster_after=roster_before,
                    message_text=(
                        "🧩 Request уже был approved ранее; persisted project "
                        "roster не менялся."
                    ),
                )
            if request.status == "rejected":
                return HireApprovalResult(
                    project_id=project_id,
                    request_id=request.request_id,
                    status="rejected",
                    roster_before=roster_before,
                    roster_after=roster_before,
                    message_text=(
                        "🧩 Request уже отклонён; persisted project roster "
                        "не менялся."
                    ),
                )
            updated_request = self._project_registry.reject_hire_request(
                request.request_id,
                decision.actor_user_id,
            )
            return HireApprovalResult(
                project_id=project_id,
                request_id=updated_request.request_id,
                status="rejected",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Pending hire request "
                    f"`{updated_request.request_id}` для "
                    f"`{updated_request.specialist_role}` отклонён. "
                    "Persisted project roster не менялся."
                ),
            )
        if (
            current_snapshot.policy is None
            or not current_snapshot.policy.allow_hiring
        ):
            return HireApprovalResult(
                project_id=project_id,
                request_id=request.request_id,
                status="blocked_by_policy",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Approve сейчас заблокирован policy проекта: "
                    "persisted project roster не менялся."
                ),
            )
        if request.status == "rejected":
            return HireApprovalResult(
                project_id=project_id,
                request_id=request.request_id,
                status="rejected",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Request уже отклонён; approve больше не применяется. "
                    "Persisted project roster не менялся."
                ),
            )
        if request.status == "approved":
            return HireApprovalResult(
                project_id=project_id,
                request_id=request.request_id,
                status="already_applied",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Request уже был approved ранее; persisted project "
                    "roster не менялся."
                ),
            )
        updated_request = self._project_registry.approve_hire_request(
            request.request_id,
            decision.actor_user_id,
        )
        roster_after = self._project_registry.get_project_specialist_roster(project_id)
        if roster_before.contains(updated_request.specialist_role):
            return HireApprovalResult(
                project_id=project_id,
                request_id=updated_request.request_id,
                status="already_applied",
                roster_before=roster_before,
                roster_after=roster_before,
                message_text=(
                    "🧩 Request "
                    f"`{updated_request.request_id}` отмечен как approved; "
                    "specialist уже был в persisted project roster."
                ),
            )
        return HireApprovalResult(
            project_id=project_id,
            request_id=updated_request.request_id,
            status="approved",
            roster_before=roster_before,
            roster_after=roster_after,
            message_text=(
                "🧩 Pending hire request "
                f"`{updated_request.request_id}` approved: "
                f"`{updated_request.specialist_role}` добавлен в persisted "
                "project roster."
            ),
        )

    @staticmethod
    def _validate_snapshot(snapshot: ProjectSnapshot) -> None:
        from core.project_registry import ProjectSnapshot as _ProjectSnapshot

        if not isinstance(snapshot, _ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(snapshot).__name__}"
            )
