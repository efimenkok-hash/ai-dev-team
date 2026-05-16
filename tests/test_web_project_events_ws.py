from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


def _make_app(module, tmp_path: Path, db_name: str, *, poll_interval: float = 0.01):
    return module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / db_name,
            project_events_poll_interval_seconds=poll_interval,
        )
    )


def _register_project(app, tmp_path: Path, *, project_id: str, slug: str, owner_user_id: int):
    app.state.project_registry.register_project(
        _snapshot(
            tmp_path,
            project_id=project_id,
            slug=slug,
            owner_user_id=owner_user_id,
            name=slug.replace("-", " ").title(),
            description=f"Project {slug}.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )


def test_websocket_project_events_sends_hello_first_for_valid_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-hello.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "hello"
    assert payload["project_id"] == "alpha_project"
    assert payload["surfaces"] == ["status", "history", "team", "threads"]
    assert payload["emitted_at"] > 0
    assert "detail" not in payload


def test_websocket_project_events_invalidates_status_when_persisted_status_changes(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-status.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"
        app.state.project_registry.set_project_policy(
            _policy("alpha_project", allow_hiring=False)
        )
        payload = websocket.receive_json()

    assert payload["type"] == "invalidate"
    assert payload["project_id"] == "alpha_project"
    assert payload["surfaces"] == ["status"]
    assert payload["emitted_at"] > 0
    assert "detail" not in payload


def test_websocket_project_events_invalidates_history_when_task_history_changes(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-history.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"
        app.state.state_db.record_task(
            _summary(
                task_id="task-alpha-1",
                branch="feature/task-alpha-1",
                finished_at=1000.0,
                project_id="alpha_project",
            )
        )
        payload = websocket.receive_json()

    assert payload["type"] == "invalidate"
    assert payload["surfaces"] == ["history"]


def test_websocket_project_events_invalidates_team_when_team_state_changes(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-team.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"
        app.state.project_registry.create_pending_hire_request(
            _pending_hire_request(
                request_id="hire-alpha-security",
                project_id="alpha_project",
                specialist_role="security_agent",
                reason="Auth boundary change needs approval.",
                created_at=1000.0,
            )
        )
        payload = websocket.receive_json()

    assert payload["type"] == "invalidate"
    assert payload["surfaces"] == ["team"]


def test_websocket_project_events_invalidates_threads_when_thread_state_changes(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-threads.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"
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
        payload = websocket.receive_json()

    assert payload["type"] == "invalidate"
    assert payload["surfaces"] == ["threads"]


def test_websocket_project_events_coalesces_multiple_surface_changes(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-coalesce.db", poll_interval=0.05)
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"
        app.state.project_registry.set_project_policy(
            _policy("alpha_project", allow_hiring=False)
        )
        app.state.state_db.record_task(
            _summary(
                task_id="task-alpha-1",
                branch="feature/task-alpha-1",
                finished_at=1000.0,
                project_id="alpha_project",
            )
        )
        app.state.project_registry.add_project_specialist(
            "alpha_project",
            "security_agent",
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
        payload = websocket.receive_json()

    assert payload["type"] == "invalidate"
    assert payload["surfaces"] == ["status", "history", "team", "threads"]


def test_websocket_project_events_isolated_to_subscribed_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-isolation.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )
    _register_project(
        app,
        tmp_path,
        project_id="beta_project",
        slug="beta-project",
        owner_user_id=202,
    )

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        assert websocket.receive_json()["type"] == "hello"
        app.state.project_registry.add_project_specialist(
            "beta_project",
            "security_agent",
        )
        time.sleep(0.05)
        app.state.state_db.record_task(
            _summary(
                task_id="task-alpha-1",
                branch="feature/task-alpha-1",
                finished_at=1000.0,
                project_id="alpha_project",
            )
        )
        payload = websocket.receive_json()

    assert payload["type"] == "invalidate"
    assert payload["project_id"] == "alpha_project"
    assert payload["surfaces"] == ["history"]


@pytest.mark.parametrize(
    ("path", "project_id", "detail"),
    [
        ("/ws/events", "", "missing_project_id"),
        (
            "/ws/events?project_id=ghost_project",
            "ghost_project",
            "unknown_project_id:ghost_project",
        ),
        (
            "/ws/events?project_id=alpha-project",
            "alpha-project",
            "unknown_project_id:alpha-project",
        ),
    ],
)
def test_websocket_project_events_error_paths_are_truthful_and_close_connection(
    tmp_path,
    monkeypatch,
    path,
    project_id,
    detail,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-error.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )

    with TestClient(app) as client, client.websocket_connect(path) as websocket:
        payload = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()

    assert payload["type"] == "error"
    assert payload["project_id"] == project_id
    assert payload["detail"] == detail
    assert payload["surfaces"] == []
    assert payload["emitted_at"] > 0


def test_websocket_project_events_does_not_break_existing_http_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = _make_app(module, tmp_path, "ws-non-regression.db")
    _register_project(
        app,
        tmp_path,
        project_id="alpha_project",
        slug="alpha-project",
        owner_user_id=101,
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-1",
            branch="feature/task-alpha-1",
            finished_at=1000.0,
            project_id="alpha_project",
        )
    )
    app.state.project_registry.add_project_specialist("alpha_project", "security_agent")
    app.state.project_registry.create_pending_hire_request(
        _pending_hire_request(
            request_id="hire-alpha-devops",
            project_id="alpha_project",
            specialist_role="devops_agent",
            reason="Runtime reliability risks need approval.",
            created_at=1002.0,
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

    with TestClient(app) as client, client.websocket_connect(
        "/ws/events?project_id=alpha_project"
    ) as websocket:
        hello = websocket.receive_json()
        projects_response = client.get("/api/projects")
        status_response = client.get("/api/projects/alpha_project/status")
        history_response = client.get("/api/projects/alpha_project/history")
        team_response = client.get("/api/projects/alpha_project/team")
        threads_response = client.get("/api/projects/alpha_project/threads")
        health_response = client.get("/healthz")
        ready_response = client.get("/readyz")

    assert hello["type"] == "hello"
    assert projects_response.status_code == 200
    assert status_response.status_code == 200
    assert history_response.status_code == 200
    assert team_response.status_code == 200
    assert threads_response.status_code == 200
    assert health_response.status_code == 200
    assert ready_response.status_code == 200
