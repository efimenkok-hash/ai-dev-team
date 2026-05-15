from __future__ import annotations

from dataclasses import dataclass

from core.agent_role_catalog import SPECIALIST_ROLE_ORDER

_SPECIALIST_ORDER_INDEX = {
    role: index for index, role in enumerate(SPECIALIST_ROLE_ORDER)
}


def _normalize_specialist_role(role: str) -> str:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("empty_specialist_role")
    normalized = role.strip().lower()
    if normalized not in _SPECIALIST_ORDER_INDEX:
        raise ValueError(f"unknown_specialist_role:{normalized}")
    return normalized


def _normalize_non_empty_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


def _normalize_text_tuple(
    values: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name}_must_be_tuple")
    if not values:
        raise ValueError(f"empty_{field_name}")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _normalize_non_empty_text(value, field_name=field_name)
        if cleaned in seen:
            raise ValueError(f"duplicate_{field_name}:{cleaned}")
        seen.add(cleaned)
        normalized.append(cleaned)
    return tuple(normalized)


@dataclass(frozen=True)
class SpecializationKnowledgeEntry:
    specialist_role: str
    domain_summary: str
    relevant_when: tuple[str, ...]
    focus_areas: tuple[str, ...]
    non_goals: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specialist_role",
            _normalize_specialist_role(self.specialist_role),
        )
        object.__setattr__(
            self,
            "domain_summary",
            _normalize_non_empty_text(
                self.domain_summary,
                field_name="domain_summary",
            ),
        )
        object.__setattr__(
            self,
            "relevant_when",
            _normalize_text_tuple(
                self.relevant_when,
                field_name="relevant_when",
            ),
        )
        object.__setattr__(
            self,
            "focus_areas",
            _normalize_text_tuple(
                self.focus_areas,
                field_name="focus_areas",
            ),
        )
        object.__setattr__(
            self,
            "non_goals",
            _normalize_text_tuple(
                self.non_goals,
                field_name="non_goals",
            ),
        )


@dataclass(frozen=True)
class SpecializationKnowledgeBase:
    entries: tuple[SpecializationKnowledgeEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise ValueError("specialization_kb_entries_must_be_tuple")
        normalized: list[SpecializationKnowledgeEntry] = []
        seen_roles: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, SpecializationKnowledgeEntry):
                raise ValueError(
                    "invalid_specialization_knowledge_entry_type:"
                    f"{type(entry).__name__}"
                )
            if entry.specialist_role in seen_roles:
                raise ValueError(
                    "duplicate_specialization_kb_entry:"
                    f"{entry.specialist_role}"
                )
            seen_roles.add(entry.specialist_role)
            normalized.append(entry)
        normalized.sort(
            key=lambda entry: _SPECIALIST_ORDER_INDEX[entry.specialist_role]
        )
        object.__setattr__(self, "entries", tuple(normalized))

    def has_role(self, role: str) -> bool:
        if not isinstance(role, str):
            return False
        normalized = role.strip().lower()
        return any(entry.specialist_role == normalized for entry in self.entries)

    def for_role(self, role: str) -> SpecializationKnowledgeEntry:
        normalized = _normalize_specialist_role(role)
        for entry in self.entries:
            if entry.specialist_role == normalized:
                return entry
        raise KeyError(f"unknown_specialist_role:{normalized}")


_DEFAULT_SPECIALIZATION_KB = SpecializationKnowledgeBase(
    entries=(
        SpecializationKnowledgeEntry(
            specialist_role="security_agent",
            domain_summary=(
                "Security specialist for auth, trust boundaries, secret handling, "
                "abuse cases, and hardening priorities."
            ),
            relevant_when=(
                "The task touches authentication, authorization, roles, or permission boundaries.",
                "The task handles secrets, tokens, credentials, sessions, or sensitive configuration.",
                "The task introduces external inputs, integrations, admin surfaces, or security-sensitive defaults.",
            ),
            focus_areas=(
                "Authn/authz correctness and privilege boundaries.",
                "Secret storage, exposure risk, and credential handling.",
                "Threat modeling, abuse cases, and dangerous defaults.",
                "Hardening priorities and realistic severity framing.",
            ),
            non_goals=(
                "Not a mandate to redesign the whole product around theoretical attacks.",
                "Not a claim that an exploit already exists without evidence in context.",
                "Not a substitute for implementation ownership, testing, or deployment execution.",
            ),
        ),
        SpecializationKnowledgeEntry(
            specialist_role="devops_agent",
            domain_summary=(
                "DevOps specialist for deployability, CI/CD correctness, environment "
                "configuration, observability, rollback, and runtime reliability."
            ),
            relevant_when=(
                "The task changes deployment flow, release packaging, or operational runtime behavior.",
                "The task depends on environment variables, infra assumptions, workers, services, or networking.",
                "The task raises questions about monitoring, incident recovery, rollout safety, or rollback paths.",
            ),
            focus_areas=(
                "CI/CD and release safety.",
                "Environment/config correctness and runtime assumptions.",
                "Observability, alerts, rollback, and recovery paths.",
                "Operational failure modes and service reliability risks.",
            ),
            non_goals=(
                "Not a promise that infrastructure already exists or is configured correctly.",
                "Not a reason to invent production topology that the context does not provide.",
                "Not a replacement for actually applying deploy, infra, or incident changes.",
            ),
        ),
        SpecializationKnowledgeEntry(
            specialist_role="data_agent",
            domain_summary=(
                "Data specialist for schema correctness, migrations, lineage, "
                "analytics semantics, ingestion/output consistency, and data quality."
            ),
            relevant_when=(
                "The task changes schemas, migrations, persistence shape, or database-facing contracts.",
                "The task affects analytics events, aggregations, derived metrics, or reporting semantics.",
                "The task depends on data pipelines, ingestion/output consistency, or data invariants.",
            ),
            focus_areas=(
                "Schema correctness, migrations, and compatibility risks.",
                "Lineage assumptions, invariants, and data shape consistency.",
                "Analytics/event semantics and aggregation correctness.",
                "Ingestion/output reliability and data quality failure modes.",
            ),
            non_goals=(
                "Not a claim that historical data is already clean or migrated.",
                "Not a reason to invent new analytics products or warehouse architecture without context.",
                "Not a substitute for running real migrations, backfills, or validation jobs.",
            ),
        ),
    )
)


def default_specialization_kb() -> SpecializationKnowledgeBase:
    return _DEFAULT_SPECIALIZATION_KB


def for_role(role: str) -> SpecializationKnowledgeEntry:
    return _DEFAULT_SPECIALIZATION_KB.for_role(role)


def has_role(role: str) -> bool:
    return _DEFAULT_SPECIALIZATION_KB.has_role(role)
