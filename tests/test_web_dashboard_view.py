from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

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


def test_dashboard_renders_truthful_empty_state_without_fake_projects(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "empty.db"))

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AI Dev Team Web Office" in response.text
    assert "Project control plane" in response.text
    assert "No persisted projects yet." in response.text
    assert "fake sample cards" in response.text
    assert "Alpha Project" not in response.text


def test_dashboard_renders_persisted_projects_in_deterministic_order_with_truthful_bindings(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "happy.db"))
    registry = app.state.project_registry
    registry.register_project(
        _snapshot(
            tmp_path,
            project_id="zeta_project",
            slug="zeta-project",
            owner_user_id=303,
            name="Zeta Project",
            description="Last project.",
            status="archived",
            with_policy=False,
            with_chat_binding=False,
            with_runtime_binding=False,
        )
    )
    registry.register_project(
        _snapshot(
            tmp_path,
            project_id="alpha_project",
            slug="alpha-project",
            owner_user_id=101,
            name="Alpha Project",
            description="First project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert body.index("Alpha Project") < body.index("Zeta Project")
    assert "alpha_project" in body
    assert "alpha-project" in body
    assert "First project." in body
    assert "Owner 101" in body
    assert "active" in body
    assert "Policy wired" in body
    assert "Chat bound" in body
    assert "Runtime bound" in body
    assert "zeta_project" in body
    assert "zeta-project" in body
    assert "Last project." in body
    assert "Owner 303" in body
    assert "archived" in body
    assert "Policy missing" in body
    assert "Chat unbound" in body
    assert "Runtime unbound" in body
    assert "Total projects" in body
    assert "Active projects" in body
    assert "Runtime wired" in body
    assert "/api/projects/alpha_project/team" not in body
    assert "Pending hire requests" not in body
    assert "Task history" not in body
    assert "Thread list" not in body


def test_dashboard_does_not_break_existing_web_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "non-regression.db",
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
    assert projects.status_code == 200
    assert status.status_code == 200
    assert history.status_code == 200
    assert team.status_code == 200
    assert threads.status_code == 200
    assert health.status_code == 200
    assert ready.status_code == 200
    assert hello["type"] == "hello"
    assert hello["project_id"] == "alpha_project"
