from __future__ import annotations

from dataclasses import dataclass

from core.agent_role_catalog import SPECIALIST_ROLE_ORDER
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
from core.specialization_hints import SpecializationHints

_SPECIALIST_ORDER_INDEX = {
    role: index for index, role in enumerate(SPECIALIST_ROLE_ORDER)
}
_ALLOWED_STATUSES = frozenset(
    {"no_candidates", "already_satisfied", "hired", "blocked_by_policy"}
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
        raise ValueError("empty_logical_hire_reason")
    return reason.strip()


def _render_roles(roles: tuple[str, ...]) -> str:
    if not roles:
        return "none"
    return ", ".join(f"`{role}`" for role in roles)


def _is_duplicate_project_specialist_error(
    exc: ValueError,
    *,
    project_id: str,
    specialist_role: str,
) -> bool:
    return str(exc) == f"duplicate_project_specialist:{project_id}:{specialist_role}"


@dataclass(frozen=True)
class LogicalHireCandidate:
    specialist_role: str
    reason: str

    def __post_init__(self) -> None:
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


@dataclass(frozen=True)
class LogicalHiringPlan:
    project_id: str
    current_roster: ProjectSpecialistRoster
    candidates: tuple[LogicalHireCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_project_id(self.project_id),
        )
        if not isinstance(self.current_roster, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_project_specialist_roster_type:"
                f"{type(self.current_roster).__name__}"
            )
        if self.current_roster.project_id != self.project_id:
            raise ValueError(
                "logical_hiring_plan_project_id_mismatch:"
                f"{self.current_roster.project_id}!={self.project_id}"
            )
        if not isinstance(self.candidates, tuple):
            raise ValueError("logical_hiring_candidates_must_be_tuple")
        normalized_candidates: list[LogicalHireCandidate] = []
        seen_roles: set[str] = set()
        for candidate in self.candidates:
            if not isinstance(candidate, LogicalHireCandidate):
                raise ValueError(
                    "invalid_logical_hire_candidate_type:"
                    f"{type(candidate).__name__}"
                )
            if candidate.specialist_role in seen_roles:
                raise ValueError(
                    "duplicate_logical_hire_candidate:"
                    f"{candidate.specialist_role}"
                )
            seen_roles.add(candidate.specialist_role)
            normalized_candidates.append(candidate)
        normalized_candidates.sort(
            key=lambda candidate: _SPECIALIST_ORDER_INDEX[candidate.specialist_role]
        )
        object.__setattr__(self, "candidates", tuple(normalized_candidates))


@dataclass(frozen=True)
class LogicalHiringResult:
    project_id: str
    status: str
    initial_roster: ProjectSpecialistRoster
    final_roster: ProjectSpecialistRoster
    hired_roles: tuple[str, ...]
    message_text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_project_id(self.project_id),
        )
        if self.status not in _ALLOWED_STATUSES:
            raise ValueError(f"invalid_logical_hiring_status:{self.status!r}")
        if not isinstance(self.initial_roster, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_initial_project_specialist_roster_type:"
                f"{type(self.initial_roster).__name__}"
            )
        if not isinstance(self.final_roster, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_final_project_specialist_roster_type:"
                f"{type(self.final_roster).__name__}"
            )
        if self.initial_roster.project_id != self.project_id:
            raise ValueError(
                "logical_hiring_initial_roster_project_id_mismatch:"
                f"{self.initial_roster.project_id}!={self.project_id}"
            )
        if self.final_roster.project_id != self.project_id:
            raise ValueError(
                "logical_hiring_final_roster_project_id_mismatch:"
                f"{self.final_roster.project_id}!={self.project_id}"
            )
        if not isinstance(self.hired_roles, tuple):
            raise ValueError("logical_hiring_hired_roles_must_be_tuple")
        normalized_hired_roles: list[str] = []
        seen_roles: set[str] = set()
        for role in self.hired_roles:
            normalized_role = _normalize_specialist_role(role)
            if normalized_role in seen_roles:
                raise ValueError(
                    f"duplicate_logical_hiring_hired_role:{normalized_role}"
                )
            seen_roles.add(normalized_role)
            normalized_hired_roles.append(normalized_role)
        normalized_hired_roles.sort(key=lambda role: _SPECIALIST_ORDER_INDEX[role])
        object.__setattr__(self, "hired_roles", tuple(normalized_hired_roles))
        if not isinstance(self.message_text, str) or not self.message_text.strip():
            raise ValueError("empty_logical_hiring_message_text")
        if self.status == "hired":
            if not self.hired_roles:
                raise ValueError("logical_hiring_hired_status_requires_roles")
            if self.final_roster == self.initial_roster:
                raise ValueError("logical_hiring_hired_status_requires_roster_change")
            if not set(self.final_roster.specialist_roles).issuperset(
                self.initial_roster.specialist_roles
            ):
                raise ValueError("logical_hiring_final_roster_must_extend_initial")
        else:
            if self.final_roster != self.initial_roster:
                raise ValueError(
                    "logical_hiring_non_hired_status_forbids_roster_change"
                )
            if self.hired_roles:
                raise ValueError(
                    "logical_hiring_non_hired_status_forbids_hired_roles"
                )


class LogicalHiringService:
    def __init__(self, project_registry: ProjectRegistry) -> None:
        if not isinstance(project_registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(project_registry).__name__}"
            )
        self._project_registry = project_registry

    @property
    def project_registry(self) -> ProjectRegistry:
        return self._project_registry

    def plan_from_hints(
        self,
        snapshot: ProjectSnapshot,
        hints: SpecializationHints,
    ) -> LogicalHiringPlan:
        if not isinstance(snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(snapshot).__name__}"
            )
        if not isinstance(hints, SpecializationHints):
            raise ValueError(
                "invalid_specialization_hints_type:"
                f"{type(hints).__name__}"
            )
        project_id = snapshot.project.project_id
        current_roster = self._project_registry.get_project_specialist_roster(
            project_id
        )
        return LogicalHiringPlan(
            project_id=project_id,
            current_roster=current_roster,
            candidates=tuple(
                LogicalHireCandidate(
                    specialist_role=hint.specialist_role,
                    reason=hint.reason,
                )
                for hint in hints.items
            ),
        )

    def apply_plan(
        self,
        snapshot: ProjectSnapshot,
        plan: LogicalHiringPlan,
    ) -> LogicalHiringResult:
        if not isinstance(snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(snapshot).__name__}"
            )
        if not isinstance(plan, LogicalHiringPlan):
            raise ValueError(
                "invalid_logical_hiring_plan_type:"
                f"{type(plan).__name__}"
            )
        project_id = snapshot.project.project_id
        if plan.project_id != project_id:
            raise ValueError(
                "logical_hiring_plan_project_id_mismatch:"
                f"{plan.project_id}!={project_id}"
            )
        current_snapshot = self._project_registry.get_project_snapshot(project_id)
        if current_snapshot is None:
            raise ValueError(f"unknown_project_id:{project_id}")
        initial_roster = self._project_registry.get_project_specialist_roster(
            project_id
        )
        if not plan.candidates:
            return LogicalHiringResult(
                project_id=project_id,
                status="no_candidates",
                initial_roster=initial_roster,
                final_roster=initial_roster,
                hired_roles=(),
                message_text=(
                    "🧩 Логический hire не требуется: `specialization_hints` "
                    "пусты, persisted project roster не изменён. Runtime "
                    "activation и owner approval flow не запускались."
                ),
            )
        if (
            current_snapshot.policy is None
            or not current_snapshot.policy.allow_hiring
        ):
            return LogicalHiringResult(
                project_id=project_id,
                status="blocked_by_policy",
                initial_roster=initial_roster,
                final_roster=initial_roster,
                hired_roles=(),
                message_text=(
                    "🧩 Логический hire заблокирован policy проекта: "
                    "persisted project roster не изменён. Runtime activation "
                    "и owner approval flow не запускались."
                ),
            )
        pending_hired_roles = tuple(
            candidate.specialist_role
            for candidate in plan.candidates
            if not initial_roster.contains(candidate.specialist_role)
        )
        if not pending_hired_roles:
            return LogicalHiringResult(
                project_id=project_id,
                status="already_satisfied",
                initial_roster=initial_roster,
                final_roster=initial_roster,
                hired_roles=(),
                message_text=(
                    "🧩 Логический hire не потребовался: все hinted "
                    "specialists уже есть в persisted project roster. "
                    "Runtime activation и owner approval flow не запускались."
                ),
            )
        hired_roles: list[str] = []
        for specialist_role in pending_hired_roles:
            try:
                self._project_registry.add_project_specialist(
                    project_id,
                    specialist_role,
                )
            except ValueError as exc:
                if not _is_duplicate_project_specialist_error(
                    exc,
                    project_id=project_id,
                    specialist_role=specialist_role,
                ):
                    raise
            else:
                hired_roles.append(specialist_role)
        final_roster = self._project_registry.get_project_specialist_roster(project_id)
        if not hired_roles:
            return LogicalHiringResult(
                project_id=project_id,
                status="already_satisfied",
                initial_roster=final_roster,
                final_roster=final_roster,
                hired_roles=(),
                message_text=(
                    "🧩 Логический hire не потребовался: все hinted "
                    "specialists уже есть в persisted project roster. "
                    "Runtime activation и owner approval flow не запускались."
                ),
            )
        return LogicalHiringResult(
            project_id=project_id,
            status="hired",
            initial_roster=initial_roster,
            final_roster=final_roster,
            hired_roles=tuple(hired_roles),
            message_text=(
                "🧩 Логический hire выполнен: в persisted project roster "
                f"добавлены {_render_roles(tuple(hired_roles))} по PM "
                "specialization_hints. Runtime activation и owner approval "
                "flow не запускались."
            ),
        )

    def run_from_hints(
        self,
        snapshot: ProjectSnapshot,
        hints: SpecializationHints,
    ) -> LogicalHiringResult:
        return self.apply_plan(
            snapshot,
            self.plan_from_hints(snapshot, hints),
        )
