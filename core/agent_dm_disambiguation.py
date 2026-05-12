"""
core/agent_dm_disambiguation.py

Typed project disambiguation for secondary owner-agent Telegram DMs.

Scope through roadmap step E4.5:
1. Resolve a direct owner-agent DM onto one project without silent guessing.
2. Reuse one active owner+agent session as the current-project anchor.
3. Support explicit project selection via `project <slug>: <message>`.
4. Return a truthful agent reply when multiple projects remain ambiguous.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from core.coordinator_role import COORDINATOR_ROLE
from core.owner_dm_routing import OwnerDmRoutingService
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB
from core.telegram_bridge import BridgeReply, IncomingMessage

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EXPLICIT_PROJECT_RE = re.compile(
    r"^\s*project\s+([a-z0-9]+(?:-[a-z0-9]+)*)\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_VALID_STATUSES = frozenset({"resolved", "ambiguous", "not_applicable"})
_VALID_RESOLUTION_SOURCES = frozenset(
    {
        "owner_dm_single_project",
        "explicit_project_slug",
        "active_agent_session",
        "single_candidate",
        "ambiguous_multiple_projects",
        "explicit_slug_not_found",
        "no_agent_dm_projects",
    }
)


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _normalize_role(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"empty_{field_name}")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}:{normalized}")
    if not _ROLE_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_project_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"empty_{field_name}")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}:{normalized}")
    if not _PROJECT_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_slug(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"empty_{field_name}")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}:{normalized}")
    if not _SLUG_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


def _normalize_timestamp(value: float | None, *, field_name: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


@dataclass(frozen=True)
class AgentDmProjectCandidate:
    project_id: str
    project_slug: str
    project_name: str
    has_active_session: bool
    last_interaction_at: float | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_project_id(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "project_slug",
            _normalize_slug(self.project_slug, field_name="project_slug"),
        )
        object.__setattr__(
            self,
            "project_name",
            _normalize_text(self.project_name, field_name="project_name"),
        )
        if not isinstance(self.has_active_session, bool):
            raise ValueError(
                "invalid_has_active_session_type:"
                f"{type(self.has_active_session).__name__}"
            )
        object.__setattr__(
            self,
            "last_interaction_at",
            _normalize_timestamp(
                self.last_interaction_at,
                field_name="last_interaction_at",
            ),
        )


@dataclass(frozen=True)
class AgentDmDisambiguationContext:
    owner_user_id: int
    dm_chat_id: int
    agent_role: str
    thread_bot_role: str
    owner_text: str

    def __post_init__(self) -> None:
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
            "dm_chat_id",
            _validate_positive_int(self.dm_chat_id, field_name="dm_chat_id"),
        )
        object.__setattr__(
            self,
            "agent_role",
            _normalize_role(self.agent_role, field_name="agent_role"),
        )
        object.__setattr__(
            self,
            "thread_bot_role",
            _normalize_role(
                self.thread_bot_role,
                field_name="thread_bot_role",
            ),
        )
        object.__setattr__(
            self,
            "owner_text",
            _normalize_text(self.owner_text, field_name="owner_text"),
        )
        if self.dm_chat_id != self.owner_user_id:
            raise ValueError(
                "owner_dm_requires_private_chat_shape:"
                f"{self.dm_chat_id}!={self.owner_user_id}"
            )
        if self.agent_role != self.thread_bot_role:
            raise ValueError(
                "agent_role_thread_bot_role_mismatch:"
                f"{self.agent_role}!={self.thread_bot_role}"
            )


@dataclass(frozen=True)
class AgentDmProjectResolution:
    status: str
    snapshot: ProjectSnapshot | None
    normalized_owner_text: str
    resolution_source: str
    candidate_projects: tuple[AgentDmProjectCandidate, ...]
    selected_project_slug: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in _VALID_STATUSES:
            raise ValueError(f"invalid_resolution_status:{self.status!r}")
        if (
            not isinstance(self.resolution_source, str)
            or self.resolution_source not in _VALID_RESOLUTION_SOURCES
        ):
            raise ValueError(
                "invalid_resolution_source:"
                f"{self.resolution_source!r}"
            )
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            ProjectSnapshot,
        ):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        object.__setattr__(
            self,
            "normalized_owner_text",
            _normalize_text(
                self.normalized_owner_text,
                field_name="normalized_owner_text",
            ),
        )
        if not isinstance(self.candidate_projects, tuple):
            raise ValueError("candidate_projects_must_be_tuple")
        normalized_candidates: list[AgentDmProjectCandidate] = []
        for candidate in self.candidate_projects:
            if not isinstance(candidate, AgentDmProjectCandidate):
                raise ValueError(
                    "invalid_agent_dm_project_candidate_type:"
                    f"{type(candidate).__name__}"
                )
            normalized_candidates.append(candidate)
        object.__setattr__(
            self,
            "candidate_projects",
            tuple(normalized_candidates),
        )
        if self.selected_project_slug is not None:
            object.__setattr__(
                self,
                "selected_project_slug",
                _normalize_slug(
                    self.selected_project_slug,
                    field_name="selected_project_slug",
                ),
            )
        if self.status == "resolved":
            if self.snapshot is None:
                raise ValueError("resolved_resolution_requires_snapshot")
            if self.selected_project_slug is None:
                raise ValueError("resolved_resolution_requires_selected_project_slug")
        if self.status != "resolved" and self.snapshot is not None:
            raise ValueError("non_resolved_resolution_forbids_snapshot")


class AgentDmDisambiguationService:
    def __init__(
        self,
        state_db: StateDB,
        project_registry: ProjectRegistry,
    ) -> None:
        if not isinstance(state_db, StateDB):
            raise ValueError(
                f"invalid_state_db_type:{type(state_db).__name__}"
            )
        if not isinstance(project_registry, ProjectRegistry):
            raise ValueError(
                "invalid_project_registry_type:"
                f"{type(project_registry).__name__}"
            )
        self._state_db = state_db
        self._project_registry = project_registry
        self._owner_dm_routing = OwnerDmRoutingService()

    def is_secondary_owner_dm_candidate(self, msg: IncomingMessage) -> bool:
        if not isinstance(msg, IncomingMessage):
            return False
        if not self._owner_dm_routing.is_owner_dm_message(msg):
            return False
        if msg.incoming_bot_role is None:
            return False
        if msg.incoming_bot_role == COORDINATOR_ROLE:
            return False
        return not (msg.text is None or not msg.text.strip())

    def build_context(
        self,
        msg: IncomingMessage,
    ) -> AgentDmDisambiguationContext:
        if not isinstance(msg, IncomingMessage):
            raise ValueError(
                "invalid_incoming_message_type:"
                f"{type(msg).__name__}"
            )
        if msg.incoming_bot_role is None:
            raise ValueError("missing_incoming_bot_role")
        return AgentDmDisambiguationContext(
            owner_user_id=msg.user_id,
            dm_chat_id=msg.chat_id,
            agent_role=msg.incoming_bot_role,
            thread_bot_role=msg.incoming_bot_role,
            owner_text=msg.text or "",
        )

    def collect_candidate_projects(
        self,
        owner_user_id: int,
        agent_role: str,
    ) -> tuple[AgentDmProjectCandidate, ...]:
        normalized_owner_user_id = _validate_positive_int(
            owner_user_id,
            field_name="owner_user_id",
        )
        normalized_agent_role = _normalize_role(
            agent_role,
            field_name="agent_role",
        )
        sessions_by_project_id = {
            session.project_id: session
            for session in self._state_db.list_agent_dm_sessions_for_owner(
                normalized_owner_user_id
            )
            if session.agent_role == normalized_agent_role
        }
        candidates: list[AgentDmProjectCandidate] = []
        for snapshot in self._project_registry.list_project_snapshots():
            project = snapshot.project
            if project.owner_user_id != normalized_owner_user_id:
                continue
            if project.status != "active":
                continue
            if snapshot.policy is None or not snapshot.policy.allow_agent_dm:
                continue
            session = sessions_by_project_id.get(project.project_id)
            has_active_session = session is not None and session.status == "active"
            candidates.append(
                AgentDmProjectCandidate(
                    project_id=project.project_id,
                    project_slug=project.slug,
                    project_name=project.name,
                    has_active_session=has_active_session,
                    last_interaction_at=(
                        session.last_interaction_at
                        if has_active_session
                        else None
                    ),
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: candidate.project_slug,
            )
        )

    def resolve(
        self,
        msg: IncomingMessage,
    ) -> AgentDmProjectResolution:
        if not isinstance(msg, IncomingMessage):
            raise ValueError(
                "invalid_incoming_message_type:"
                f"{type(msg).__name__}"
            )
        if (
            self.is_secondary_owner_dm_candidate(msg)
            and msg.project_context_source == "owner_dm_single_project"
            and msg.project_id is not None
        ):
            snapshot = self._project_registry.get_project_snapshot(msg.project_id)
            if snapshot is not None:
                return AgentDmProjectResolution(
                    status="resolved",
                    snapshot=snapshot,
                    normalized_owner_text=msg.text or "",
                    resolution_source="owner_dm_single_project",
                    candidate_projects=(),
                    selected_project_slug=snapshot.project.slug,
                )
        if not self.is_secondary_owner_dm_candidate(msg):
            return AgentDmProjectResolution(
                status="not_applicable",
                snapshot=None,
                normalized_owner_text=msg.text or "not applicable",
                resolution_source="no_agent_dm_projects",
                candidate_projects=(),
                selected_project_slug=None,
            )

        context = self.build_context(msg)
        candidates = self.collect_candidate_projects(
            context.owner_user_id,
            context.agent_role,
        )
        if not candidates:
            return AgentDmProjectResolution(
                status="not_applicable",
                snapshot=None,
                normalized_owner_text=context.owner_text,
                resolution_source="no_agent_dm_projects",
                candidate_projects=(),
                selected_project_slug=None,
            )

        explicit_selection = self._parse_explicit_project_selection(
            context.owner_text
        )
        candidates_by_slug = {
            candidate.project_slug: candidate for candidate in candidates
        }
        if explicit_selection is not None:
            selected_slug, normalized_owner_text = explicit_selection
            candidate = candidates_by_slug.get(selected_slug)
            if candidate is None:
                return AgentDmProjectResolution(
                    status="ambiguous",
                    snapshot=None,
                    normalized_owner_text=normalized_owner_text,
                    resolution_source="explicit_slug_not_found",
                    candidate_projects=candidates,
                    selected_project_slug=selected_slug,
                )
            snapshot = self._project_registry.get_project_snapshot(candidate.project_id)
            if snapshot is None:
                raise ValueError(
                    "explicit_project_candidate_snapshot_missing:"
                    f"{candidate.project_id}"
                )
            return AgentDmProjectResolution(
                status="resolved",
                snapshot=snapshot,
                normalized_owner_text=normalized_owner_text,
                resolution_source="explicit_project_slug",
                candidate_projects=candidates,
                selected_project_slug=candidate.project_slug,
            )

        active_session_candidate = self._resolve_active_session_candidate(
            candidates
        )
        if active_session_candidate is not None:
            snapshot = self._project_registry.get_project_snapshot(
                active_session_candidate.project_id
            )
            if snapshot is None:
                raise ValueError(
                    "active_session_candidate_snapshot_missing:"
                    f"{active_session_candidate.project_id}"
                )
            return AgentDmProjectResolution(
                status="resolved",
                snapshot=snapshot,
                normalized_owner_text=context.owner_text,
                resolution_source="active_agent_session",
                candidate_projects=candidates,
                selected_project_slug=active_session_candidate.project_slug,
            )

        if len(candidates) == 1:
            snapshot = self._project_registry.get_project_snapshot(
                candidates[0].project_id
            )
            if snapshot is None:
                raise ValueError(
                    "single_candidate_snapshot_missing:"
                    f"{candidates[0].project_id}"
                )
            return AgentDmProjectResolution(
                status="resolved",
                snapshot=snapshot,
                normalized_owner_text=context.owner_text,
                resolution_source="single_candidate",
                candidate_projects=candidates,
                selected_project_slug=candidates[0].project_slug,
            )

        return AgentDmProjectResolution(
            status="ambiguous",
            snapshot=None,
            normalized_owner_text=context.owner_text,
            resolution_source="ambiguous_multiple_projects",
            candidate_projects=candidates,
            selected_project_slug=None,
        )

    def format_ambiguous_reply(
        self,
        resolution: AgentDmProjectResolution,
        *,
        agent_role: str,
    ) -> BridgeReply:
        if not isinstance(resolution, AgentDmProjectResolution):
            raise ValueError(
                "invalid_agent_dm_project_resolution_type:"
                f"{type(resolution).__name__}"
            )
        normalized_agent_role = _normalize_role(
            agent_role,
            field_name="agent_role",
        )
        if resolution.status != "ambiguous":
            raise ValueError(
                f"ambiguous_reply_requires_ambiguous_resolution:{resolution.status}"
            )
        project_lines = "\n".join(
            f"• {candidate.project_slug} — {candidate.project_name}"
            for candidate in resolution.candidate_projects
        )
        if resolution.resolution_source == "explicit_slug_not_found":
            body = (
                "Не вижу такой project slug среди доступных direct-DM проектов.\n"
                "\n"
                f"Получил slug: `{resolution.selected_project_slug}`\n"
                "\n"
                "Повтори сообщение в формате:\n"
                "`project <slug>: <текст>`\n"
                "\n"
                "Доступные проекты:\n"
                f"{project_lines}\n"
                "\n"
                "Это не запускало project pipeline."
            )
        else:
            body = (
                "У тебя несколько проектов для этого личного agent DM, и я не "
                "выбрал проект автоматически.\n"
                "\n"
                "Повтори сообщение в формате:\n"
                "`project <slug>: <текст>`\n"
                "\n"
                "Доступные проекты:\n"
                f"{project_lines}\n"
                "\n"
                "Это не запускало project pipeline."
            )
        return BridgeReply(
            persona_role=normalized_agent_role,
            body=body,
        )

    def _parse_explicit_project_selection(
        self,
        owner_text: str,
    ) -> tuple[str, str] | None:
        normalized_owner_text = _normalize_text(
            owner_text,
            field_name="owner_text",
        )
        match = _EXPLICIT_PROJECT_RE.fullmatch(normalized_owner_text)
        if match is None:
            return None
        return (
            _normalize_slug(match.group(1), field_name="selected_project_slug"),
            _normalize_text(match.group(2), field_name="normalized_owner_text"),
        )

    @staticmethod
    def _resolve_active_session_candidate(
        candidates: tuple[AgentDmProjectCandidate, ...],
    ) -> AgentDmProjectCandidate | None:
        active_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.has_active_session
        )
        if len(active_candidates) != 1:
            return None
        return active_candidates[0]
