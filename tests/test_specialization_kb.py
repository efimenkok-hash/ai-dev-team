import pytest

from core.agent_role_catalog import is_runtime_exposed_agent_role
from core.specialization_kb import (
    SpecializationKnowledgeBase,
    SpecializationKnowledgeEntry,
    default_specialization_kb,
    for_role,
    has_role,
)


def _entry(role: str, *, domain_summary: str | None = None):
    return SpecializationKnowledgeEntry(
        specialist_role=role,
        domain_summary=(
            domain_summary if domain_summary is not None else f"{role} summary"
        ),
        relevant_when=("Relevant situation A", "Relevant situation B"),
        focus_areas=("Focus area A", "Focus area B"),
        non_goals=("Non-goal A", "Non-goal B"),
    )


def test_default_specialization_kb_has_exactly_three_entries_in_catalog_order():
    kb = default_specialization_kb()

    assert len(kb.entries) == 3
    assert tuple(entry.specialist_role for entry in kb.entries) == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )


def test_default_specialization_kb_for_role_returns_valid_entry():
    entry = default_specialization_kb().for_role("security_agent")

    assert entry.specialist_role == "security_agent"
    assert "auth" in entry.domain_summary.lower()
    assert len(entry.relevant_when) >= 1
    assert len(entry.focus_areas) >= 1
    assert len(entry.non_goals) >= 1


def test_default_specialization_kb_has_role_helper():
    assert has_role("security_agent") is True
    assert has_role("ghost_agent") is False


def test_module_level_for_role_helper():
    entry = for_role("devops_agent")
    assert entry.specialist_role == "devops_agent"


def test_kb_normalizes_order_deterministically():
    kb = SpecializationKnowledgeBase(
        (
            _entry("data_agent"),
            _entry("security_agent"),
            _entry("devops_agent"),
        )
    )

    assert tuple(entry.specialist_role for entry in kb.entries) == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )


@pytest.mark.parametrize(
    "role",
    ("planning_agent", "coordinator_agent", "ghost_agent"),
)
def test_entry_rejects_non_specialist_role(role):
    with pytest.raises(ValueError, match=fr"unknown_specialist_role:{role}"):
        _entry(role)


@pytest.mark.parametrize("summary", ("", "   "))
def test_entry_rejects_empty_domain_summary(summary):
    with pytest.raises(ValueError, match="empty_domain_summary"):
        _entry("security_agent", domain_summary=summary)


def test_entry_rejects_non_tuple_relevant_when():
    with pytest.raises(ValueError, match="relevant_when_must_be_tuple"):
        SpecializationKnowledgeEntry(
            specialist_role="security_agent",
            domain_summary="summary",
            relevant_when=["A"],  # type: ignore[arg-type]
            focus_areas=("Focus",),
            non_goals=("Non-goal",),
        )


def test_entry_rejects_duplicate_relevant_when():
    with pytest.raises(ValueError, match="duplicate_relevant_when:A"):
        SpecializationKnowledgeEntry(
            specialist_role="security_agent",
            domain_summary="summary",
            relevant_when=("A", "A"),
            focus_areas=("Focus",),
            non_goals=("Non-goal",),
        )


def test_entry_rejects_empty_focus_areas():
    with pytest.raises(ValueError, match="empty_focus_areas"):
        SpecializationKnowledgeEntry(
            specialist_role="devops_agent",
            domain_summary="summary",
            relevant_when=("A",),
            focus_areas=(),
            non_goals=("Non-goal",),
        )


def test_entry_rejects_duplicate_focus_areas():
    with pytest.raises(ValueError, match="duplicate_focus_areas:Focus"):
        SpecializationKnowledgeEntry(
            specialist_role="devops_agent",
            domain_summary="summary",
            relevant_when=("A",),
            focus_areas=("Focus", "Focus"),
            non_goals=("Non-goal",),
        )


def test_entry_rejects_empty_non_goals():
    with pytest.raises(ValueError, match="empty_non_goals"):
        SpecializationKnowledgeEntry(
            specialist_role="data_agent",
            domain_summary="summary",
            relevant_when=("A",),
            focus_areas=("Focus",),
            non_goals=(),
        )


def test_entry_rejects_duplicate_non_goals():
    with pytest.raises(ValueError, match="duplicate_non_goals:Non-goal"):
        SpecializationKnowledgeEntry(
            specialist_role="data_agent",
            domain_summary="summary",
            relevant_when=("A",),
            focus_areas=("Focus",),
            non_goals=("Non-goal", "Non-goal"),
        )


def test_kb_rejects_duplicate_entries_for_same_role():
    with pytest.raises(
        ValueError,
        match="duplicate_specialization_kb_entry:security_agent",
    ):
        SpecializationKnowledgeBase(
            (
                _entry("security_agent", domain_summary="A"),
                _entry("security_agent", domain_summary="B"),
            )
        )


def test_kb_for_role_rejects_unknown_role_honestly():
    with pytest.raises(ValueError, match="unknown_specialist_role:planning_agent"):
        default_specialization_kb().for_role("planning_agent")


def test_kb_does_not_change_runtime_exposed_role_semantics():
    assert has_role("security_agent") is True
    assert is_runtime_exposed_agent_role("security_agent") is True
    assert is_runtime_exposed_agent_role("devops_agent") is False
