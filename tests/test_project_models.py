"""Tests for core.project_models."""

from __future__ import annotations

import pytest

from core.project_models import (
    VALID_CHAT_PROVIDERS,
    VALID_MEMBER_TYPES,
    VALID_MEMBERSHIP_STATUSES,
    VALID_PROJECT_STATUSES,
    Project,
    ProjectChatBinding,
    ProjectMembership,
    ProjectPolicy,
)


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _membership(**overrides: object) -> ProjectMembership:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "member_id": "coordinator_01",
        "member_type": "agent",
        "role_name": "coordinator_agent",
        "status": "active",
    }
    data.update(overrides)
    return ProjectMembership(**data)


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_id": -1001234567890,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def test_valid_constants_are_stable():
    assert VALID_PROJECT_STATUSES == {"draft", "active", "archived"}
    assert VALID_MEMBER_TYPES == {"owner", "human", "agent", "bot"}
    assert VALID_MEMBERSHIP_STATUSES == {"pending", "active", "inactive"}
    assert VALID_CHAT_PROVIDERS == {"telegram"}


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


def test_project_happy_path_normalizes_fields():
    project = _project(
        project_id="  Alpha_Project  ",
        slug="  Alpha-Project  ",
        name="  Alpha Project  ",
        description="  Main delivery workspace.  ",
        status="  Active  ",
    )

    assert project.project_id == "alpha_project"
    assert project.slug == "alpha-project"
    assert project.name == "Alpha Project"
    assert project.description == "Main delivery workspace."
    assert project.status == "active"


def test_project_is_frozen():
    project = _project()
    with pytest.raises(Exception):
        project.name = "Other"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "  ", None, "alpha-project", "123alpha", "русский"])
def test_project_rejects_invalid_project_id(bad: object):
    with pytest.raises(ValueError):
        _project(project_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "alpha_project", "-alpha", "русский"])
def test_project_rejects_invalid_slug(bad: object):
    with pytest.raises(ValueError):
        _project(slug=bad)


@pytest.mark.parametrize("field_name", ["name", "description"])
def test_project_rejects_empty_text_fields(field_name: str):
    with pytest.raises(ValueError, match=f"empty_{field_name}"):
        _project(**{field_name: "  "})


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "7"])
def test_project_rejects_invalid_owner_user_id(bad: object):
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        _project(owner_user_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "paused", "русский"])
def test_project_rejects_invalid_status(bad: object):
    with pytest.raises(ValueError):
        _project(status=bad)


# ---------------------------------------------------------------------------
# ProjectPolicy
# ---------------------------------------------------------------------------


def test_project_policy_happy_path_normalizes_project_id():
    policy = _policy(project_id="  Alpha_Project  ")
    assert policy.project_id == "alpha_project"
    assert policy.allow_hiring is True
    assert policy.allow_agent_dm is False
    assert policy.require_owner_approval_for_hires is True


def test_project_policy_is_frozen():
    policy = _policy()
    with pytest.raises(Exception):
        policy.allow_hiring = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "bad"),
    [
        ("allow_hiring", 1),
        ("allow_agent_dm", "yes"),
        ("require_owner_approval_for_hires", None),
    ],
)
def test_project_policy_rejects_non_bool_flags(field_name: str, bad: object):
    with pytest.raises(ValueError, match=f"invalid_{field_name}_type"):
        _policy(**{field_name: bad})


def test_project_policy_rejects_invalid_project_id():
    with pytest.raises(ValueError, match="invalid_project_id"):
        _policy(project_id="bad-id")


# ---------------------------------------------------------------------------
# ProjectMembership
# ---------------------------------------------------------------------------


def test_project_membership_happy_path_normalizes_fields():
    membership = _membership(
        project_id="  Alpha_Project  ",
        member_id="  Coordinator_01  ",
        member_type="  Agent  ",
        role_name="  Coordinator_Agent  ",
        status="  Pending  ",
    )

    assert membership.project_id == "alpha_project"
    assert membership.member_id == "coordinator_01"
    assert membership.member_type == "agent"
    assert membership.role_name == "coordinator_agent"
    assert membership.status == "pending"


def test_project_membership_is_frozen():
    membership = _membership()
    with pytest.raises(Exception):
        membership.role_name = "writer_agent"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "  ", None, "coordinator-01", "русский"])
def test_project_membership_rejects_invalid_member_id(bad: object):
    with pytest.raises(ValueError):
        _membership(member_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "service", "русский"])
def test_project_membership_rejects_invalid_member_type(bad: object):
    with pytest.raises(ValueError):
        _membership(member_type=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "coordinator-agent", "русский"])
def test_project_membership_rejects_invalid_role_name(bad: object):
    with pytest.raises(ValueError):
        _membership(role_name=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "paused", "русский"])
def test_project_membership_rejects_invalid_status(bad: object):
    with pytest.raises(ValueError):
        _membership(status=bad)


# ---------------------------------------------------------------------------
# ProjectChatBinding
# ---------------------------------------------------------------------------


def test_project_chat_binding_happy_path_normalizes_provider():
    binding = _binding(project_id="  Alpha_Project  ", chat_provider="  Telegram  ")
    assert binding.project_id == "alpha_project"
    assert binding.chat_id == -1001234567890
    assert binding.chat_provider == "telegram"


def test_project_chat_binding_uses_telegram_default():
    binding = ProjectChatBinding(project_id="alpha_project", chat_id=-42)
    assert binding.chat_provider == "telegram"


def test_project_chat_binding_accepts_positive_chat_id():
    binding = _binding(chat_id=42)
    assert binding.chat_id == 42


def test_project_chat_binding_is_frozen():
    binding = _binding()
    with pytest.raises(Exception):
        binding.chat_id = 1  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, True, 1.2, "7"])
def test_project_chat_binding_rejects_invalid_chat_id(bad: object):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        _binding(chat_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "discord", "русский"])
def test_project_chat_binding_rejects_invalid_provider(bad: object):
    with pytest.raises(ValueError):
        _binding(chat_provider=bad)
