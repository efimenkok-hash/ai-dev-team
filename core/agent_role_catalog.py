"""
core/agent_role_catalog.py

Canonical catalog of known logical backend agent roles.

Scope for roadmap step G1.1:
1. Keep the baseline internal team definition explicit and unchanged.
2. Introduce specialist roles as first-class known logical roles.
3. Distinguish selectable logical roles from runtime-exposed Telegram roles.
4. Provide a small shared contract for role classification without changing
   runtime activation semantics.
"""

from __future__ import annotations

BASELINE_INTERNAL_TEAM_ROLE_ORDER: tuple[str, ...] = (
    "coordinator_agent",
    "planning_agent",
    "pm_agent",
    "architect_agent",
    "writer_agent",
    "reviewer_agent",
    "tester_agent",
    "qa_agent",
    "fixer_agent",
)

SPECIALIST_ROLE_ORDER: tuple[str, ...] = (
    "security_agent",
    "devops_agent",
    "data_agent",
)

# Specialists remain logical non-baseline roles. Only explicitly promoted
# specialist identities may become optional live Telegram identities.
OPTIONAL_LIVE_SPECIALIST_ROLE_ORDER: tuple[str, ...] = (
    "security_agent",
    "devops_agent",
    "data_agent",
)

# Selectable logical roles are LLM-backed internal participants that can be
# truthfully selected/consulted without becoming runtime-exposed Telegram
# identities. This excludes coordinator_agent at this step because the
# baseline pipeline contract and dispatcher registry remain worker-oriented.
SELECTABLE_AGENT_ROLE_ORDER: tuple[str, ...] = (
    "planning_agent",
    "pm_agent",
    "architect_agent",
    "writer_agent",
    "reviewer_agent",
    "tester_agent",
    "qa_agent",
    "fixer_agent",
    "security_agent",
    "devops_agent",
    "data_agent",
)
SELECTABLE_AGENT_ROLES: frozenset[str] = frozenset(SELECTABLE_AGENT_ROLE_ORDER)

KNOWN_AGENT_ROLES: frozenset[str] = frozenset(
    BASELINE_INTERNAL_TEAM_ROLE_ORDER + SPECIALIST_ROLE_ORDER
)

# Runtime-exposed identities include the baseline internal team plus the
# narrowly promoted specialist subset that is allowed to appear as separate
# live Telegram bots when explicitly mapped in TELEGRAM_AGENT_TOKENS.
RUNTIME_EXPOSED_AGENT_ROLE_ORDER: tuple[str, ...] = (
    BASELINE_INTERNAL_TEAM_ROLE_ORDER + OPTIONAL_LIVE_SPECIALIST_ROLE_ORDER
)
RUNTIME_EXPOSED_AGENT_ROLES: frozenset[str] = frozenset(
    RUNTIME_EXPOSED_AGENT_ROLE_ORDER
)


def _validate_catalog_integrity() -> None:
    baseline = set(BASELINE_INTERNAL_TEAM_ROLE_ORDER)
    specialists = set(SPECIALIST_ROLE_ORDER)
    if len(baseline) != len(BASELINE_INTERNAL_TEAM_ROLE_ORDER):
        raise RuntimeError("duplicate_baseline_internal_team_role")
    if len(specialists) != len(SPECIALIST_ROLE_ORDER):
        raise RuntimeError("duplicate_specialist_role")
    overlap = baseline & specialists
    if overlap:
        raise RuntimeError(
            f"overlapping_agent_roles:{','.join(sorted(overlap))}"
        )
    selectable = set(SELECTABLE_AGENT_ROLE_ORDER)
    if len(selectable) != len(SELECTABLE_AGENT_ROLE_ORDER):
        raise RuntimeError("duplicate_selectable_agent_role")
    promoted_specialists = set(OPTIONAL_LIVE_SPECIALIST_ROLE_ORDER)
    if len(promoted_specialists) != len(OPTIONAL_LIVE_SPECIALIST_ROLE_ORDER):
        raise RuntimeError("duplicate_optional_live_specialist_role")
    if not promoted_specialists <= specialists:
        raise RuntimeError("optional_live_specialist_role_not_specialist")
    expected_selectable = (
        set(BASELINE_INTERNAL_TEAM_ROLE_ORDER) - {"coordinator_agent"}
    ) | specialists
    if selectable != expected_selectable:
        raise RuntimeError("selectable_agent_roles_catalog_mismatch")
    if baseline | specialists != KNOWN_AGENT_ROLES:
        raise RuntimeError("known_agent_roles_catalog_mismatch")
    expected_runtime_exposed = baseline | promoted_specialists
    if set(RUNTIME_EXPOSED_AGENT_ROLE_ORDER) != expected_runtime_exposed:
        raise RuntimeError("runtime_exposed_agent_roles_catalog_mismatch")


def is_known_agent_role(role: str) -> bool:
    return isinstance(role, str) and role in KNOWN_AGENT_ROLES


def is_specialist_role(role: str) -> bool:
    return isinstance(role, str) and role in SPECIALIST_ROLE_ORDER


def is_baseline_internal_team_role(role: str) -> bool:
    return isinstance(role, str) and role in BASELINE_INTERNAL_TEAM_ROLE_ORDER


def is_selectable_agent_role(role: str) -> bool:
    return isinstance(role, str) and role in SELECTABLE_AGENT_ROLES


def is_runtime_exposed_agent_role(role: str) -> bool:
    return isinstance(role, str) and role in RUNTIME_EXPOSED_AGENT_ROLES


_validate_catalog_integrity()
