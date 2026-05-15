from __future__ import annotations

from dataclasses import dataclass

from core.agent_role_catalog import (
    BASELINE_INTERNAL_TEAM_ROLE_ORDER,
    SPECIALIST_ROLE_ORDER,
)

_IDENTIFIER_ORDER = {
    role: index for index, role in enumerate(SPECIALIST_ROLE_ORDER)
}


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


def _normalize_specialist_role(specialist_role: str) -> str:
    if not isinstance(specialist_role, str) or not specialist_role.strip():
        raise ValueError("empty_specialist_role")
    normalized = specialist_role.strip().lower()
    if normalized not in _IDENTIFIER_ORDER:
        raise ValueError(f"unknown_specialist_role:{normalized}")
    return normalized


@dataclass(frozen=True)
class ProjectSpecialistAssignment:
    project_id: str
    specialist_role: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _normalize_project_id(self.project_id))
        object.__setattr__(
            self,
            "specialist_role",
            _normalize_specialist_role(self.specialist_role),
        )


@dataclass(frozen=True)
class ProjectSpecialistRoster:
    project_id: str
    specialist_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _normalize_project_id(self.project_id))
        if not isinstance(self.specialist_roles, tuple):
            raise ValueError("specialist_roles_must_be_tuple")
        normalized_roles: list[str] = []
        seen_roles: set[str] = set()
        for role in self.specialist_roles:
            normalized_role = _normalize_specialist_role(role)
            if normalized_role in seen_roles:
                raise ValueError(f"duplicate_specialist_role:{normalized_role}")
            seen_roles.add(normalized_role)
            normalized_roles.append(normalized_role)
        normalized_roles.sort(key=lambda role: _IDENTIFIER_ORDER[role])
        object.__setattr__(self, "specialist_roles", tuple(normalized_roles))

    @property
    def is_empty(self) -> bool:
        return not self.specialist_roles

    def contains(self, role: str) -> bool:
        if not isinstance(role, str):
            return False
        normalized = role.strip().lower()
        return normalized in self.specialist_roles

    def resolved_team_roles(self) -> tuple[str, ...]:
        return BASELINE_INTERNAL_TEAM_ROLE_ORDER + self.specialist_roles
