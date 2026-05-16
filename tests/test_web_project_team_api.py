from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from core.agent_role_catalog import BASELINE_INTERNAL_TEAM_ROLE_ORDER
from core.hire_approval import PendingHireRequest
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.task_history import TaskSummary


def _import_web_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    sys.modules.pop("web.main", None)
    return importlib.import_module("web.main")


def _project(
    *,
    project_id: str,
    slug: str,
    owner_user_id: int,
    name: str,
    description: str,
    status: str = "active",
) -> Project:
    return Project(
        project_id=project_id,
        slug=slug,
        name=name,
        description=description,
        owner_user_id=owner_user_id,
        status=status,
    )


def _policy(project_id: str) -> ProjectPolicy:
    return ProjectPolicy(
        project_id=project_id,
        allow_hiring=True,
        allow_agent_dm=False,
        require_owner_approval_for_hires=True,
    )


def _runtime_binding(project_id: str, repo_path: Path) -> ProjectRuntimeBinding:
    return ProjectRuntimeBinding(
        project_id=project_id,
        adapter_name=f"{project_id}_adapter",
        repo_path=repo_path,
        worktree_root=repo_path.parent / f"{project_id}-worktrees",
        base_branch="main",
        branch_prefix="feature/",
        language="python",
        rules=(),
        commands=(),
        forbidden_paths=(),
        forbidden_tokens=(),
    )


def _chat_binding(project_id: str, chat_id: int) -> ProjectChatBinding:
    return ProjectChatBinding(
        project_id=project_id,
        chat_provider="telegram",
        chat_id=chat_id,
    )


def _pending_hire_request(
    *,
    request_id: str,
    project_id: str,
    specialist_role: str,
    reason: str,
    created_at: float,
    source: str = "logical_hiring_pm_hint",
) -> PendingHireRequest:
    return PendingHireRequest(
        request_id=request_id,
        project_id=project_id,
        specialist_role=specialist_role,
        reason=reason,
        source=source,
        status="pending",
        created_at=created_at,
    )


def _summary(
    *,
    task_id: str,
    branch: str,
    finished_at: float,
    project_id: str,
) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        branch=branch,
        commit_sha="abc123def456789",
        final_state="SUCCESS",
        failure_reason=None,
        tier_name="PREMIUM",
        finished_at=finished_at,
        project_id=project_id,
    )


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _snapshot(
    tmp_path: Path,
    *,
    project_id: str,
    slug: str,
    owner_user_id: int,
    name: str,
    description: str,
    status: str = "active",
    with_policy: bool,
    with_chat_binding: bool,
    with_runtime_binding: bool,
) -> ProjectSnapshot:
    repo = _make_repo(tmp_path, f"{project_id}-repo")
    return ProjectSnapshot(
        project=_project(
            project_id=project_id,
            slug=slug,
            owner_user_id=owner_user_id,
            name=name,
            description=description,
            status=status,
        ),
        policy=_policy(project_id) if with_policy else None,
        chat_binding=(
            _chat_binding(project_id, -100000000000 - owner_user_id)
            if with_chat_binding
            else None
        ),
        runtime_binding=(
            _runtime_binding(project_id, repo)
            if with_runtime_binding
            else None
        ),
    )


def test_api_project_team_returns_truthful_empty_specialist_state(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "empty-team.db")
    )
    app.state.project_registry.register_project(
        _snapshot(
            tmp_path,
            project_id="alpha_project",
            slug="alpha-project",
            owner_user_id=101,
            name="Alpha Project",
            description="Primary project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/alpha_project/team")

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "alpha_project",
        "baseline_internal_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "project_specialist_roster": [],
        "resolved_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "pending_hire_requests": [],
    }


def test_api_project_team_returns_approved_specialists_and_pending_hires_separately(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "team-state.db")
    )
    registry = app.state.project_registry
    registry.register_project(
        _snapshot(
            tmp_path,
            project_id="alpha_project",
            slug="alpha-project",
            owner_user_id=101,
            name="Alpha Project",
            description="Primary project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )
    registry.add_project_specialist("alpha_project", "data_agent")
    registry.add_project_specialist("alpha_project", "security_agent")
    registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-alpha-devops",
            project_id="alpha_project",
            specialist_role="devops_agent",
            reason="Deployment and rollback risk is material.",
            created_at=1001.0,
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/alpha_project/team")

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "alpha_project",
        "baseline_internal_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "project_specialist_roster": [
            "security_agent",
            "data_agent",
        ],
        "resolved_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER)
        + ["security_agent", "data_agent"],
        "pending_hire_requests": [
            {
                "request_id": "hire-alpha-devops",
                "specialist_role": "devops_agent",
                "reason": "Deployment and rollback risk is material.",
                "source": "logical_hiring_pm_hint",
                "created_at": 1001.0,
            },
        ],
    }


def test_api_project_team_isolates_roster_and_pending_state_per_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "isolated-team.db")
    )
    registry = app.state.project_registry
    registry.register_project(
        _snapshot(
            tmp_path,
            project_id="alpha_project",
            slug="alpha-project",
            owner_user_id=101,
            name="Alpha Project",
            description="Primary project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )
    registry.register_project(
        _snapshot(
            tmp_path,
            project_id="beta_project",
            slug="beta-project",
            owner_user_id=202,
            name="Beta Project",
            description="Secondary project.",
            with_policy=False,
            with_chat_binding=False,
            with_runtime_binding=True,
        )
    )
    registry.add_project_specialist("alpha_project", "security_agent")
    registry.add_project_specialist("beta_project", "devops_agent")
    registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-alpha-data",
            project_id="alpha_project",
            specialist_role="data_agent",
            reason="Data quality risks need approval.",
            created_at=1000.0,
        )
    )
    registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-beta-security",
            project_id="beta_project",
            specialist_role="security_agent",
            reason="Auth boundary changes are in scope.",
            created_at=1001.0,
        )
    )

    with TestClient(app) as client:
        alpha_response = client.get("/api/projects/alpha_project/team")
        beta_response = client.get("/api/projects/beta_project/team")

    assert alpha_response.status_code == 200
    assert alpha_response.json() == {
        "project_id": "alpha_project",
        "baseline_internal_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "project_specialist_roster": ["security_agent"],
        "resolved_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER)
        + ["security_agent"],
        "pending_hire_requests": [
            {
                "request_id": "hire-alpha-data",
                "specialist_role": "data_agent",
                "reason": "Data quality risks need approval.",
                "source": "logical_hiring_pm_hint",
                "created_at": 1000.0,
            }
        ],
    }
    assert beta_response.status_code == 200
    assert beta_response.json() == {
        "project_id": "beta_project",
        "baseline_internal_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "project_specialist_roster": ["devops_agent"],
        "resolved_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER)
        + ["devops_agent"],
        "pending_hire_requests": [
            {
                "request_id": "hire-beta-security",
                "specialist_role": "security_agent",
                "reason": "Auth boundary changes are in scope.",
                "source": "logical_hiring_pm_hint",
                "created_at": 1001.0,
            }
        ],
    }


def test_api_project_team_returns_truthful_404_for_unknown_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "unknown-team.db")
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/ghost_project/team")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:ghost_project",
    }


def test_api_project_team_treats_slug_like_path_as_truthful_miss(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "slug-like-team.db")
    )
    app.state.project_registry.register_project(
        _snapshot(
            tmp_path,
            project_id="alpha_project",
            slug="alpha-project",
            owner_user_id=101,
            name="Alpha Project",
            description="Primary project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/alpha-project/team")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:alpha-project",
    }


def test_api_project_team_does_not_break_projects_status_history_health_or_ready(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    db_path = tmp_path / "team-non-regression.db"
    app = module.create_app(module.WebAppConfig(state_db_path=db_path))
    app.state.project_registry.register_project(
        _snapshot(
            tmp_path,
            project_id="alpha_project",
            slug="alpha-project",
            owner_user_id=101,
            name="Alpha Project",
            description="Primary project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=False,
        )
    )
    app.state.project_registry.add_project_specialist(
        "alpha_project",
        "security_agent",
    )
    app.state.project_registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-alpha-devops",
            project_id="alpha_project",
            specialist_role="devops_agent",
            reason="Runtime reliability risks need approval.",
            created_at=1002.0,
        )
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-1",
            branch="feature/task-alpha-1",
            finished_at=1000.0,
            project_id="alpha_project",
        )
    )

    with TestClient(app) as client:
        team_response = client.get("/api/projects/alpha_project/team")
        projects_response = client.get("/api/projects")
        status_response = client.get("/api/projects/alpha_project/status")
        history_response = client.get("/api/projects/alpha_project/history")
        health_response = client.get("/healthz")
        ready_response = client.get("/readyz")

    team_payload = team_response.json()

    assert team_response.status_code == 200
    assert "threads" not in team_payload
    assert "history" not in team_payload
    assert projects_response.status_code == 200
    assert status_response.status_code == 200
    assert history_response.status_code == 200
    assert health_response.status_code == 200
    assert ready_response.status_code == 200
