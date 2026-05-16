from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from core.agent_bus_models import ProjectThread
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


def test_api_project_threads_returns_truthful_empty_state(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "empty-threads.db")
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
        response = client.get("/api/projects/alpha_project/threads")

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "alpha_project",
        "items": [],
        "count": 0,
    }


def test_api_project_threads_returns_persisted_threads_in_truthful_order(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "threads.db")
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
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000002",
            opened_by_role="architect_agent",
            created_at=1000.0,
            last_message_at=1005.0,
            task_id="task-alpha-2",
        )
    )
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000001",
            opened_by_role="writer_agent",
            created_at=1001.0,
            last_message_at=1005.0,
            task_id=None,
        )
    )
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000003",
            opened_by_role="reviewer_agent",
            created_at=1002.0,
            last_message_at=1004.0,
            status="closed",
            task_id="task-alpha-3",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/alpha_project/threads")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "project_id": "alpha_project",
        "items": [
            {
                "thread_id": "thread_000001",
                "opened_by_role": "writer_agent",
                "status": "open",
                "created_at": 1001.0,
                "last_message_at": 1005.0,
                "task_id": None,
            },
            {
                "thread_id": "thread_000002",
                "opened_by_role": "architect_agent",
                "status": "open",
                "created_at": 1000.0,
                "last_message_at": 1005.0,
                "task_id": "task-alpha-2",
            },
            {
                "thread_id": "thread_000003",
                "opened_by_role": "reviewer_agent",
                "status": "closed",
                "created_at": 1002.0,
                "last_message_at": 1004.0,
                "task_id": "task-alpha-3",
            },
        ],
        "count": 3,
    }
    assert all(
        set(item.keys()) == {
            "thread_id",
            "opened_by_role",
            "status",
            "created_at",
            "last_message_at",
            "task_id",
        }
        for item in payload["items"]
    )


def test_api_project_threads_isolates_threads_per_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "threads-isolated.db")
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
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000001",
            opened_by_role="architect_agent",
            created_at=1000.0,
            last_message_at=1002.0,
            task_id="task-alpha",
        )
    )
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="beta_project",
            thread_id="thread_000001",
            opened_by_role="writer_agent",
            created_at=1001.0,
            last_message_at=1003.0,
            task_id="task-beta",
        )
    )

    with TestClient(app) as client:
        alpha_response = client.get("/api/projects/alpha_project/threads")
        beta_response = client.get("/api/projects/beta_project/threads")

    assert alpha_response.status_code == 200
    assert alpha_response.json() == {
        "project_id": "alpha_project",
        "items": [
            {
                "thread_id": "thread_000001",
                "opened_by_role": "architect_agent",
                "status": "open",
                "created_at": 1000.0,
                "last_message_at": 1002.0,
                "task_id": "task-alpha",
            }
        ],
        "count": 1,
    }
    assert beta_response.status_code == 200
    assert beta_response.json() == {
        "project_id": "beta_project",
        "items": [
            {
                "thread_id": "thread_000001",
                "opened_by_role": "writer_agent",
                "status": "open",
                "created_at": 1001.0,
                "last_message_at": 1003.0,
                "task_id": "task-beta",
            }
        ],
        "count": 1,
    }


def test_api_project_threads_returns_truthful_404_for_unknown_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "unknown-threads.db")
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/ghost_project/threads")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:ghost_project",
    }


def test_api_project_threads_treats_slug_like_path_as_truthful_miss(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "slug-like-threads.db")
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
        response = client.get("/api/projects/alpha-project/threads")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:alpha-project",
    }


def test_api_project_threads_does_not_break_existing_project_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    db_path = tmp_path / "threads-non-regression.db"
    app = module.create_app(module.WebAppConfig(state_db_path=db_path))
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
            with_runtime_binding=False,
        )
    )
    registry.add_project_specialist("alpha_project", "security_agent")
    registry.create_pending_hire_request(
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
    app.state.state_db.upsert_project_thread(
        _thread(
            project_id="alpha_project",
            thread_id="thread_000001",
            opened_by_role="architect_agent",
            created_at=1000.0,
            last_message_at=1001.0,
            task_id="task-alpha-1",
        )
    )

    with TestClient(app) as client:
        threads_response = client.get("/api/projects/alpha_project/threads")
        projects_response = client.get("/api/projects")
        status_response = client.get("/api/projects/alpha_project/status")
        history_response = client.get("/api/projects/alpha_project/history")
        team_response = client.get("/api/projects/alpha_project/team")
        health_response = client.get("/healthz")
        ready_response = client.get("/readyz")

    payload = threads_response.json()

    assert threads_response.status_code == 200
    assert "messages" not in payload
    assert "history" not in payload
    assert projects_response.status_code == 200
    assert status_response.status_code == 200
    assert history_response.status_code == 200
    assert team_response.status_code == 200
    assert health_response.status_code == 200
    assert ready_response.status_code == 200
    assert team_response.json()["baseline_internal_team_roles"] == list(
        BASELINE_INTERNAL_TEAM_ROLE_ORDER
    )

