"""
core/project_models.py

Foundational frozen dataclasses for the AI Office project model.

Scope for roadmap step P1.1:
1. Define immutable project-domain entities only.
2. Validate and normalize every field eagerly in __post_init__.
3. Keep the model runtime-agnostic: no StateDB wiring, no TelegramBridge
   integration, no registry, no onboarding flow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

VALID_PROJECT_STATUSES = frozenset({"draft", "active", "archived"})
VALID_MEMBER_TYPES = frozenset({"owner", "human", "agent", "bot"})
VALID_MEMBERSHIP_STATUSES = frozenset({"pending", "active", "inactive"})
VALID_CHAT_PROVIDERS = frozenset({"telegram"})


def _normalize_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


def _normalize_identifier(value: str, *, field_name: str) -> str:
    normalized = _normalize_text(value, field_name=field_name).lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_slug(value: str, *, field_name: str) -> str:
    normalized = _normalize_text(value, field_name=field_name).lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _SLUG_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_choice(
    value: str,
    *,
    field_name: str,
    allowed: frozenset[str],
) -> str:
    normalized = _normalize_identifier(value, field_name=field_name)
    if normalized not in allowed:
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _validate_non_zero_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _validate_bool(value: bool, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            f"invalid_{field_name}_type:{type(value).__name__}"
        )
    return value


@dataclass(frozen=True)
class Project:
    project_id: str
    slug: str
    name: str
    description: str
    owner_user_id: int
    status: str = "active"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "slug",
            _normalize_slug(self.slug, field_name="slug"),
        )
        object.__setattr__(
            self,
            "name",
            _normalize_text(self.name, field_name="name"),
        )
        object.__setattr__(
            self,
            "description",
            _normalize_text(self.description, field_name="description"),
        )
        object.__setattr__(
            self,
            "owner_user_id",
            _validate_positive_int(
                self.owner_user_id,
                field_name="owner_user_id",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_choice(
                self.status,
                field_name="project_status",
                allowed=VALID_PROJECT_STATUSES,
            ),
        )


@dataclass(frozen=True)
class ProjectPolicy:
    project_id: str
    allow_hiring: bool = True
    allow_agent_dm: bool = False
    require_owner_approval_for_hires: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        _validate_bool(self.allow_hiring, field_name="allow_hiring")
        _validate_bool(self.allow_agent_dm, field_name="allow_agent_dm")
        _validate_bool(
            self.require_owner_approval_for_hires,
            field_name="require_owner_approval_for_hires",
        )


@dataclass(frozen=True)
class ProjectMembership:
    project_id: str
    member_id: str
    member_type: str
    role_name: str
    status: str = "active"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "member_id",
            _normalize_identifier(self.member_id, field_name="member_id"),
        )
        object.__setattr__(
            self,
            "member_type",
            _normalize_choice(
                self.member_type,
                field_name="member_type",
                allowed=VALID_MEMBER_TYPES,
            ),
        )
        object.__setattr__(
            self,
            "role_name",
            _normalize_identifier(self.role_name, field_name="role_name"),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_choice(
                self.status,
                field_name="membership_status",
                allowed=VALID_MEMBERSHIP_STATUSES,
            ),
        )


@dataclass(frozen=True)
class ProjectChatBinding:
    project_id: str
    chat_id: int
    chat_provider: str = "telegram"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "chat_id",
            _validate_non_zero_int(self.chat_id, field_name="chat_id"),
        )
        object.__setattr__(
            self,
            "chat_provider",
            _normalize_choice(
                self.chat_provider,
                field_name="chat_provider",
                allowed=VALID_CHAT_PROVIDERS,
            ),
        )

