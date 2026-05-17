from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from core.agent_bus_models import ProjectThread
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


def _policy(
    project_id: str,
    *,
    allow_hiring: bool = True,
    allow_agent_dm: bool = False,
    require_owner_approval_for_hires: bool = True,
) -> ProjectPolicy:
    return ProjectPolicy(
        project_id=project_id,
        allow_hiring=allow_hiring,
        allow_agent_dm=allow_agent_dm,
        require_owner_approval_for_hires=require_owner_approval_for_hires,
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
) -> PendingHireRequest:
    return PendingHireRequest(
        request_id=request_id,
        project_id=project_id,
        specialist_role=specialist_role,
        reason=reason,
        source="logical_hiring_pm_hint",
        status="pending",
        created_at=created_at,
    )


def _thread(
    *,
    project_id: str,
    thread_id: str,
    opened_by_role: str,
    created_at: float,
    last_message_at: float,
    status: str = "open",
    task_id: str | None = None,
) -> ProjectThread:
    return ProjectThread(
        project_id=project_id,
        thread_id=thread_id,
        opened_by_role=opened_by_role,
        status=status,
        created_at=created_at,
        last_message_at=last_message_at,
        task_id=task_id,
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


def test_project_view_renders_truthful_happy_path_from_persisted_state(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "project-view.db",
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
    registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-alpha-devops",
            project_id="alpha_project",
            specialist_role="devops_agent",
            reason="Deployment risk requires specialist review.",
            created_at=1712345678.0,
        )
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-2",
            branch="feature/task-alpha-2",
            finished_at=2000.0,
            project_id="alpha_project",
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
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000002",
            opened_by_role="architect_agent",
            created_at=100.0,
            last_message_at=300.0,
            task_id="task-alpha-2",
        )
    )
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000001",
            opened_by_role="pm_agent",
            created_at=50.0,
            last_message_at=200.0,
            task_id="task-alpha-1",
        )
    )

    with TestClient(app) as client:
        response = client.get("/projects/alpha_project")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Alpha Project" in body
    assert "alpha_project" in body
    assert "alpha-project" in body
    assert "Primary project." in body
    assert "Owner user" in body
    assert "Project status" in body
    assert "Policy wired" in body
    assert "Chat bound" in body
    assert "Runtime bound" in body
    assert "enabled" in body
    assert "required" in body
    assert "security_agent" in body
    assert "devops_agent" in body
    assert "Resolved team size" in body
    assert "/projects/alpha_project/team" in body
    assert "/projects/alpha-project/team" not in body
    assert "/projects/alpha_project/history" in body
    assert "/projects/alpha-project/history" not in body
    assert "/projects/alpha_project/settings" in body
    assert "/projects/alpha-project/settings" not in body
    assert "Recent persisted tasks" in body
    assert "task-alpha-2" in body
    assert "feature/task-alpha-2" in body
    assert "Persisted threads" in body
    assert "thread_000002" in body
    assert "architect_agent" in body
    assert "Pending hire requests" in body
    assert "Backend threads" in body
    assert "Task history page" not in body
    assert "Settings" not in body


def test_project_view_renders_truthful_empty_sections_without_fake_activity(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "empty-sections.db"))
    app.state.project_registry.register_project(
        _snapshot(
            tmp_path,
            project_id="beta_project",
            slug="beta-project",
            owner_user_id=202,
            name="Beta Project",
            description="Secondary project.",
            with_policy=False,
            with_chat_binding=False,
            with_runtime_binding=False,
        )
    )

    with TestClient(app) as client:
        response = client.get("/projects/beta_project")

    assert response.status_code == 200
    body = response.text
    assert "Beta Project" in body
    assert "beta_project" in body
    assert "beta-project" in body
    assert "Policy missing" in body
    assert "Chat unbound" in body
    assert "Runtime unbound" in body
    assert "Policy is not configured for this project." in body
    assert "No approved specialists yet." in body
    assert "No pending hire requests." in body
    assert "No persisted task history yet." in body
    assert "No persisted threads yet." in body
    assert "task-alpha-1" not in body
    assert "thread_000001" not in body


def test_project_view_returns_truthful_404_for_unknown_and_slug_like_paths(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "404.db"))
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
        unknown = client.get("/projects/ghost_project")
        slug_like = client.get("/projects/alpha-project")

    assert unknown.status_code == 404
    assert slug_like.status_code == 404


def test_project_view_does_not_break_existing_web_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "project-view-non-regression.db",
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
    assert projects.status_code == 200
    assert status.status_code == 200
    assert history.status_code == 200
    assert team.status_code == 200
    assert threads.status_code == 200
    assert health.status_code == 200
    assert ready.status_code == 200
    assert hello["type"] == "hello"
    assert hello["project_id"] == "alpha_project"
