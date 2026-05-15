import pytest

from core.specialization_hints import (
    SpecializationHint,
    SpecializationHints,
)


def test_specialization_hint_happy_path():
    hint = SpecializationHint(
        specialist_role="security_agent",
        reason="Task touches auth boundaries.",
    )

    assert hint.specialist_role == "security_agent"
    assert hint.reason == "Task touches auth boundaries."


def test_specialization_hints_happy_path_and_deterministic_order():
    hints = SpecializationHints(
        (
            SpecializationHint("data_agent", "Needs schema review."),
            SpecializationHint("security_agent", "Touches secrets."),
            SpecializationHint("devops_agent", "Deployment assumptions matter."),
        )
    )

    assert tuple(hint.specialist_role for hint in hints.items) == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )


@pytest.mark.parametrize(
    "role",
    ("planning_agent", "coordinator_agent", "ghost_agent"),
)
def test_specialization_hint_rejects_non_specialist_role(role):
    with pytest.raises(ValueError, match=fr"unknown_specialist_role:{role}"):
        SpecializationHint(role, "Reason")


@pytest.mark.parametrize("reason", ("", "   "))
def test_specialization_hint_rejects_empty_reason(reason):
    with pytest.raises(ValueError, match="empty_specialization_reason"):
        SpecializationHint("security_agent", reason)


def test_specialization_hints_rejects_non_tuple_items():
    with pytest.raises(
        ValueError,
        match="specialization_hints_items_must_be_tuple",
    ):
        SpecializationHints([])  # type: ignore[arg-type]


def test_specialization_hints_rejects_duplicate_specialist_role():
    with pytest.raises(
        ValueError,
        match="duplicate_specialization_hint:security_agent",
    ):
        SpecializationHints(
            (
                SpecializationHint("security_agent", "Auth risk."),
                SpecializationHint("security_agent", "Secrets risk."),
            )
        )


def test_specialization_hints_pm_payload_round_trip():
    hints = SpecializationHints(
        (
            SpecializationHint("devops_agent", "Runtime reliability matters."),
            SpecializationHint("security_agent", "Auth boundary review."),
        )
    )

    payload = {"specialization_hints": hints.to_pm_payload()}
    restored = SpecializationHints.from_pm_payload(payload)

    assert restored == hints


def test_specialization_hints_from_pm_payload_rejects_missing_field():
    with pytest.raises(ValueError, match="missing_specialization_hints"):
        SpecializationHints.from_pm_payload(
            {
                "plan_id": "x",
                "task_summary": "y",
                "subtasks": [],
            }
        )


def test_specialization_hints_from_pm_payload_accepts_explicit_empty_list():
    restored = SpecializationHints.from_pm_payload(
        {
            "plan_id": "x",
            "task_summary": "y",
            "subtasks": [],
            "specialization_hints": [],
        }
    )

    assert restored == SpecializationHints.empty()


def test_specialization_hints_empty_helper():
    hints = SpecializationHints.empty()

    assert hints.is_empty is True
    assert hints.items == ()
