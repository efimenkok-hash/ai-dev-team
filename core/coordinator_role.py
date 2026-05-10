from __future__ import annotations

from core.agent_personas import AgentPersona, PersonaRegistry

COORDINATOR_ROLE = "coordinator_agent"
_COORDINATOR_ROLE_ALIASES = frozenset({COORDINATOR_ROLE, "pm_agent"})


def _normalize_role_text(role: str) -> str:
    if not isinstance(role, str):
        raise ValueError(f"invalid_coordinator_role_type:{type(role).__name__}")
    normalized = role.strip().lower()
    if not normalized:
        raise ValueError("empty_coordinator_role")
    return normalized


def is_coordinator_role(role: str) -> bool:
    normalized = _normalize_role_text(role)
    return normalized in _COORDINATOR_ROLE_ALIASES


def normalize_coordinator_role(role: str) -> str:
    normalized = _normalize_role_text(role)
    if normalized not in _COORDINATOR_ROLE_ALIASES:
        raise ValueError(f"invalid_coordinator_role:{normalized}")
    return COORDINATOR_ROLE


def resolve_coordinator_persona(registry: PersonaRegistry) -> AgentPersona:
    if not isinstance(registry, PersonaRegistry):
        raise ValueError(
            f"invalid_persona_registry_type:{type(registry).__name__}"
        )
    try:
        return registry.for_role(COORDINATOR_ROLE)
    except KeyError as exc:
        raise ValueError("missing_coordinator_persona") from exc
