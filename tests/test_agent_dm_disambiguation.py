from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.agent_dm_disambiguation import (
    AgentDmDisambiguationContext,
    AgentDmDisambiguationService,
    AgentDmProjectCandidate,
)
from core.agent_dm_models import AgentDmSession
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB
from core.telegram_bridge import BridgeReply, IncomingMessage

OWNER_ID = 101


def _db(tmp_path):
    return StateDB(tmp_path / "state.db")


def _project(**overrides):
    data = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": OWNER_ID,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(project_id: str = "alpha_project", **overrides):
    data = {
        "project_id": project_id,
        "allow_hiring": True,
        "allow_agent_dm": True,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _register_snapshot(
    registry: ProjectRegistry,
    *,
    project: Project | None = None,
    policy: ProjectPolicy | None = None,
) -> ProjectSnapshot:
    snapshot = ProjectSnapshot(
        project=_project() if project is None else project,
        policy=_policy() if policy is None else policy,
    )
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


def _msg(**overrides) -> IncomingMessage:
    data = {
        "chat_id": OWNER_ID,
        "user_id": OWNER_ID,
        "message_id": 1,
        "text": "Подскажи по API",
        "project_context_source": "none",
        "project_context_reason": "owner_dm_requires_explicit_project_chat",
        "incoming_bot_role": "writer_agent",
    }
    data.update(overrides)
    return IncomingMessage(**data)


def _session(**overrides) -> AgentDmSession:
    data = {
        "owner_user_id": OWNER_ID,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "dm_chat_id": OWNER_ID,
        "status": "active",
        "created_at": 10.0,
        "last_interaction_at": 20.0,
    }
    data.update(overrides)
    return AgentDmSession(**data)


def test_project_candidate_is_frozen():
    candidate = AgentDmProjectCandidate(
        project_id="alpha_project",
        project_slug="alpha-project",
        project_name="Alpha Project",
        has_active_session=False,
        last_interaction_at=None,
    )

    with pytest.raises(FrozenInstanceError):
        candidate.project_slug = "beta-project"  # type: ignore[misc]


def test_disambiguation_context_rejects_non_private_dm_shape():
    with pytest.raises(ValueError, match="owner_dm_requires_private_chat_shape"):
        AgentDmDisambiguationContext(
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID + 1,
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            owner_text="text",
        )


def test_secondary_private_owner_dm_is_candidate(tmp_path):
    db = _db(tmp_path)
    service = AgentDmDisambiguationService(db, ProjectRegistry(db))

    assert service.is_secondary_owner_dm_candidate(_msg()) is True
    assert service.is_secondary_owner_dm_candidate(
        _msg(incoming_bot_role="coordinator_agent")
    ) is False
    assert service.is_secondary_owner_dm_candidate(_msg(chat_id=-100123)) is False
    assert service.is_secondary_owner_dm_candidate(_msg(incoming_bot_role=None)) is False


def test_collect_candidate_projects_filters_and_orders_deterministically(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(
        registry,
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy("beta_project", allow_agent_dm=True),
    )
    _register_snapshot(
        registry,
        project=_project(
            project_id="alpha_project",
            slug="alpha-project",
            name="Alpha Project",
        ),
        policy=_policy("alpha_project", allow_agent_dm=True),
    )
    _register_snapshot(
        registry,
        project=_project(
            project_id="gamma_project",
            slug="gamma-project",
            name="Gamma Project",
        ),
        policy=_policy("gamma_project", allow_agent_dm=False),
    )
    _register_snapshot(
        registry,
        project=_project(
            project_id="archived_project",
            slug="archived-project",
            name="Archived Project",
            status="archived",
        ),
        policy=_policy("archived_project", allow_agent_dm=True),
    )
    _register_snapshot(
        registry,
        project=_project(
            project_id="other_owner_project",
            slug="other-owner-project",
            name="Other Owner Project",
            owner_user_id=202,
        ),
        policy=_policy("other_owner_project", allow_agent_dm=True),
    )
    db.upsert_agent_dm_session(
        _session(
            project_id="beta_project",
            last_interaction_at=42.0,
        )
    )
    service = AgentDmDisambiguationService(db, registry)

    candidates = service.collect_candidate_projects(OWNER_ID, "writer_agent")

    assert [candidate.project_slug for candidate in candidates] == [
        "alpha-project",
        "beta-project",
    ]
    assert candidates[0].has_active_session is False
    assert candidates[0].last_interaction_at is None
    assert candidates[1].has_active_session is True
    assert candidates[1].last_interaction_at == 42.0


def test_explicit_slug_resolution_strips_prefix(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry)
    _register_snapshot(
        registry,
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy("beta_project", allow_agent_dm=True),
    )
    service = AgentDmDisambiguationService(db, registry)

    resolution = service.resolve(
        _msg(text="project alpha-project: подскажи по API")
    )

    assert resolution.status == "resolved"
    assert resolution.resolution_source == "explicit_project_slug"
    assert resolution.snapshot is not None
    assert resolution.snapshot.project.project_id == "alpha_project"
    assert resolution.normalized_owner_text == "подскажи по API"
    assert resolution.selected_project_slug == "alpha-project"


def test_unknown_explicit_slug_returns_truthful_not_found_resolution(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry)
    _register_snapshot(
        registry,
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy("beta_project", allow_agent_dm=True),
    )
    service = AgentDmDisambiguationService(db, registry)

    resolution = service.resolve(
        _msg(text="project unknown-project: подскажи по API")
    )

    assert resolution.status == "ambiguous"
    assert resolution.resolution_source == "explicit_slug_not_found"
    assert resolution.selected_project_slug == "unknown-project"
    reply = service.format_ambiguous_reply(resolution, agent_role="writer_agent")
    assert isinstance(reply, BridgeReply)
    assert reply.persona_role == "writer_agent"
    assert "unknown-project" in reply.body
    assert "alpha-project" in reply.body
    assert "beta-project" in reply.body
    assert "project <slug>: <текст>" in reply.body


def test_one_active_session_reuses_current_project(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry)
    _register_snapshot(
        registry,
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy("beta_project", allow_agent_dm=True),
    )
    db.upsert_agent_dm_session(_session())
    service = AgentDmDisambiguationService(db, registry)

    resolution = service.resolve(_msg())

    assert resolution.status == "resolved"
    assert resolution.resolution_source == "active_agent_session"
    assert resolution.snapshot is not None
    assert resolution.snapshot.project.project_id == "alpha_project"


def test_closed_or_other_agent_sessions_do_not_anchor_current_project(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry)
    _register_snapshot(
        registry,
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy("beta_project", allow_agent_dm=True),
    )
    db.upsert_agent_dm_session(_session(status="closed"))
    db.upsert_agent_dm_session(
        _session(
            project_id="beta_project",
            agent_role="reviewer_agent",
            thread_bot_role="reviewer_agent",
        )
    )
    service = AgentDmDisambiguationService(db, registry)

    resolution = service.resolve(_msg())

    assert resolution.status == "ambiguous"
    assert resolution.resolution_source == "ambiguous_multiple_projects"


def test_ambiguous_multiple_projects_requires_explicit_slug(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry)
    _register_snapshot(
        registry,
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy("beta_project", allow_agent_dm=True),
    )
    service = AgentDmDisambiguationService(db, registry)

    resolution = service.resolve(_msg())

    assert resolution.status == "ambiguous"
    assert resolution.resolution_source == "ambiguous_multiple_projects"
    reply = service.format_ambiguous_reply(resolution, agent_role="writer_agent")
    assert "alpha-project" in reply.body
    assert "beta-project" in reply.body
    assert "project <slug>: <текст>" in reply.body
    assert "не выбрал проект автоматически" in reply.body.lower()
