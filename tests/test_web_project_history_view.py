from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

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


def _summary(
    *,
    task_id: str,
    branch: str,
    commit_sha: str | None = "abc123def456789",
    final_state: str = "SUCCESS",
    failure_reason: str | None = None,
    tier_name: str = "PREMIUM",
    finished_at: float = 1000.0,
    project_id: str | None = None,
) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        branch=branch,
        commit_sha=commit_sha,
        final_state=final_state,
        failure_reason=failure_reason,
        tier_name=tier_name,
        finished_at=finished_at,
        project_id=project_id,
    )


def test_project_history_view_renders_truthful_persisted_tasks_newest_first(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "history-view.db",
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
    registry.register_project(
        _snapshot(
            tmp_path,
            project_id="beta_project",
            slug="beta-project",
            owner_user_id=202,
            name="Beta Project",
            description="Secondary project.",
            with_policy=True,
            with_chat_binding=True,
            with_runtime_binding=True,
        )
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-newer",
            branch="feature/task-alpha-newer",
            commit_sha="feedface12345678",
            final_state="SUCCESS",
            failure_reason=None,
            tier_name="PREMIUM",
            finished_at=1004.0,
            project_id="alpha_project",
        )
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-beta-only",
            branch="feature/task-beta-only",
            finished_at=1002.0,
            project_id="beta_project",
        )
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-legacy",
            branch="feature/task-legacy",
            finished_at=1003.0,
            project_id=None,
        )
    )
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-older",
            branch="feature/task-alpha-older",
            commit_sha=None,
            final_state="FAIL",
            failure_reason="lint_failed",
            tier_name="ECONOMY",
            finished_at=1001.0,
            project_id="alpha_project",
        )
    )

    with TestClient(app) as client:
        response = client.get("/projects/alpha_project/history")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Alpha Project" in body
    assert "alpha_project" in body
    assert "/projects/alpha_project" in body
    assert "Project history" in body
    assert "Persisted task history only." in body
    assert "Recent persisted tasks" in body
    assert body.index("task-alpha-newer") < body.index("task-alpha-older")
    assert "feature/task-alpha-newer" in body
    assert "SUCCESS" in body
    assert "PREMIUM" in body
    assert "feedface12345678" in body
    assert "task-alpha-older" in body
    assert "FAIL" in body
    assert "ECONOMY" in body
    assert "Failure reason: lint_failed" in body
    assert "task-beta-only" not in body
    assert "task-legacy" not in body
    assert "thread_000001" not in body
    assert "security_agent" not in body
    assert "connected bot" not in body


def test_project_history_view_renders_truthful_empty_state_without_fake_activity(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "history-view-empty.db")
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
        response = client.get("/projects/beta_project/history")

    assert response.status_code == 200
    body = response.text
    assert "Beta Project" in body
    assert "beta_project" in body
    assert "No persisted task history yet." in body
    assert "task-alpha-1" not in body
    assert "feature/" not in body
    assert "sample task" not in body.lower()


def test_project_history_view_surfaces_failure_detail_preview(
    tmp_path,
    monkeypatch,
):
    from core.task_history import compose_failure_reason

    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "history-view-detail.db",
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
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-diagnostics",
            branch="feature/task-alpha-diagnostics",
            commit_sha=None,
            final_state="FAIL",
            failure_reason=compose_failure_reason(
                "review_fix_loop_exceeded",
                "review=REJECTED; next fix: src/example.py: restore square implementation",
            ),
            tier_name="ECONOMY",
            finished_at=1005.0,
            project_id="alpha_project",
        )
    )

    with TestClient(app) as client:
        response = client.get("/projects/alpha_project/history")
        history = client.get("/api/projects/alpha_project/history")

    assert response.status_code == 200
    assert history.status_code == 200
    body = response.text
    assert "Failure reason: review_fix_loop_exceeded" in body
    assert (
        "Failure detail: review=REJECTED; next fix: src/example.py: restore square implementation"
        in body
    )
    payload = history.json()
    assert payload["count"] == 1
    assert payload["items"][0]["failure_reason"] == "review_fix_loop_exceeded"
    assert (
        payload["items"][0]["failure_detail"]
        == "review=REJECTED; next fix: src/example.py: restore square implementation"
    )


def test_project_history_view_returns_truthful_404_for_unknown_and_slug_like_paths(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "history-view-404.db")
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

    with TestClient(app, raise_server_exceptions=False) as client:
        unknown = client.get("/projects/ghost_project/history")
        slug_like = client.get("/projects/alpha-project/history")

    assert unknown.status_code == 404
    assert slug_like.status_code == 404


def test_project_history_view_does_not_break_existing_web_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "history-view-non-regression.db",
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
        history_view = client.get("/projects/alpha_project/history")
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
    assert history_view.status_code == 200
    assert projects.status_code == 200
    assert status.status_code == 200
    assert history.status_code == 200
    assert team.status_code == 200
    assert threads.status_code == 200
    assert health.status_code == 200
    assert ready.status_code == 200
    assert hello["type"] == "hello"
    assert hello["project_id"] == "alpha_project"
