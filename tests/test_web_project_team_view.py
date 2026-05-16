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


def test_project_team_view_renders_truthful_team_state_with_clear_separation(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "team-view.db",
            project_events_poll_interval_seconds=0.01,
        )
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
    registry.add_project_specialist("alpha_project", "security_agent")
    registry.add_project_specialist("alpha_project", "data_agent")
    registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-alpha-devops",
            project_id="alpha_project",
            specialist_role="devops_agent",
            reason="Deployment risk requires specialist review.",
            created_at=1712345678.0,
        )
    )

    with TestClient(app) as client:
        response = client.get("/projects/alpha_project/team")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Alpha Project" in body
    assert "alpha_project" in body
    assert "Project team" in body
    assert "/projects/alpha_project" in body
    assert "Baseline internal team" in body
    assert "Approved specialists" in body
    assert "Pending hire requests" in body
    assert "Resolved team size" in body
    assert "Persisted logical team state only." in body
    for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER:
        assert f'data-baseline-role="{role}"' in body
        assert f'data-resolved-role="{role}"' in body
    assert 'data-approved-role="security_agent"' in body
    assert 'data-approved-role="data_agent"' in body
    assert 'data-pending-role="devops_agent"' in body
    assert 'data-resolved-role="security_agent"' in body
    assert 'data-resolved-role="data_agent"' in body
    assert 'data-resolved-role="devops_agent"' not in body
    assert "hire-alpha-devops" in body
    assert "logical_hiring_pm_hint" in body
    assert "Deployment risk requires specialist review." in body
    assert "connected bot" not in body
    assert "active in chat" not in body


def test_project_team_view_renders_truthful_empty_specialist_and_pending_states(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "team-view-empty.db")
    )
    app.state.project_registry.register_project(
        _snapshot(
            tmp_path,
            project_id="beta_project",
            slug="beta-project",
            owner_user_id=202,
            name="Beta Project",
            description="Secondary project.",
            with_policy=True,
            with_chat_binding=False,
            with_runtime_binding=False,
        )
    )

    with TestClient(app) as client:
        response = client.get("/projects/beta_project/team")

    assert response.status_code == 200
    body = response.text
    assert "Beta Project" in body
    assert "beta_project" in body
    assert "No approved specialists yet." in body
    assert "No pending hire requests." in body
    for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER:
        assert f'data-baseline-role="{role}"' in body
    assert 'data-approved-role="' not in body
    assert 'data-pending-role="' not in body
    assert "sample specialist" not in body.lower()


def test_project_team_view_returns_truthful_404_for_unknown_and_slug_like_paths(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "team-view-404.db"))
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

    with TestClient(app, raise_server_exceptions=False) as client:
        unknown = client.get("/projects/ghost_project/team")
        slug_like = client.get("/projects/alpha-project/team")

    assert unknown.status_code == 404
    assert slug_like.status_code == 404


def test_project_team_view_does_not_break_existing_web_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "team-view-non-regression.db",
            project_events_poll_interval_seconds=0.01,
        )
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
        dashboard = client.get("/")
        project_view = client.get("/projects/alpha_project")
        team_view = client.get("/projects/alpha_project/team")
        projects = client.get("/api/projects")
        status = client.get("/api/projects/alpha_project/status")
        history = client.get("/api/projects/alpha_project/history")
        team = client.get("/api/projects/alpha_project/team")
        threads = client.get("/api/projects/alpha_project/threads")
        health = client.get("/healthz")
        ready = client.get("/readyz")
        with client.websocket_connect("/ws/events?project_id=alpha_project") as websocket:
            hello = websocket.receive_json()

    assert dashboard.status_code == 200
    assert project_view.status_code == 200
    assert team_view.status_code == 200
    assert projects.status_code == 200
    assert status.status_code == 200
    assert history.status_code == 200
    assert team.status_code == 200
    assert threads.status_code == 200
    assert health.status_code == 200
    assert ready.status_code == 200
    assert hello["type"] == "hello"
    assert hello["project_id"] == "alpha_project"
