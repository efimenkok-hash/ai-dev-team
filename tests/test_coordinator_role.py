import pytest

from core.agent_personas import default_registry
from core.coordinator_role import (
    COORDINATOR_ROLE,
    is_coordinator_role,
    normalize_coordinator_role,
    resolve_coordinator_persona,
)


def test_coordinator_role_constant_is_canonical():
    assert COORDINATOR_ROLE == "coordinator_agent"


def test_is_coordinator_role_accepts_canonical_role():
    assert is_coordinator_role("coordinator_agent") is True


def test_is_coordinator_role_accepts_legacy_pm_alias():
    assert is_coordinator_role("pm_agent") is True


@pytest.mark.parametrize("bad", [None, 123, True])
def test_is_coordinator_role_rejects_invalid_inputs(bad):
    with pytest.raises(ValueError, match="invalid_coordinator_role_type"):
        is_coordinator_role(bad)  # type: ignore[arg-type]


def test_is_coordinator_role_returns_false_for_unknown_string():
    assert is_coordinator_role("ceo_agent") is False


def test_normalize_coordinator_role_returns_canonical_role():
    assert normalize_coordinator_role("coordinator_agent") == COORDINATOR_ROLE
    assert normalize_coordinator_role("pm_agent") == COORDINATOR_ROLE


@pytest.mark.parametrize("bad", ["", "   ", "ceo_agent"])
def test_normalize_coordinator_role_rejects_invalid_strings(bad):
    match = "empty_coordinator_role" if not bad.strip() else "invalid_coordinator_role"
    with pytest.raises(ValueError, match=match):
        normalize_coordinator_role(bad)


def test_resolve_coordinator_persona_returns_default_registry_persona():
    persona = resolve_coordinator_persona(default_registry())

    assert persona.agent_role == COORDINATOR_ROLE
    assert persona.human_name == "Координатор"


def test_resolve_coordinator_persona_rejects_bad_registry():
    with pytest.raises(ValueError, match="invalid_persona_registry_type"):
        resolve_coordinator_persona("bad")  # type: ignore[arg-type]
