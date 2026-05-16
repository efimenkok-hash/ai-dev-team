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


def test_api_project_history_returns_truthful_empty_history_for_existing_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "empty-history.db")
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
        response = client.get("/api/projects/alpha_project/history")

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "alpha_project",
        "items": [],
        "count": 0,
    }


def test_api_project_history_returns_project_scoped_persisted_tasks_newest_first(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "project-history.db")
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

    with TestClient(app) as client:
        response = client.get("/api/projects/alpha_project/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "project_id": "alpha_project",
        "items": [
            {
                "task_id": "task-alpha-newer",
                "branch": "feature/task-alpha-newer",
                "commit_sha": "feedface12345678",
                "final_state": "SUCCESS",
                "failure_reason": None,
                "tier_name": "PREMIUM",
                "finished_at": 1004.0,
            },
            {
                "task_id": "task-alpha-older",
                "branch": "feature/task-alpha-older",
                "commit_sha": None,
                "final_state": "FAIL",
                "failure_reason": "lint_failed",
                "tier_name": "ECONOMY",
                "finished_at": 1001.0,
            },
        ],
        "count": 2,
    }
    assert all(
        set(item.keys()) == {
            "task_id",
            "branch",
            "commit_sha",
            "final_state",
            "failure_reason",
            "tier_name",
            "finished_at",
        }
        for item in payload["items"]
    )


def test_api_project_history_returns_truthful_404_for_unknown_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "unknown-history.db")
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/ghost_project/history")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:ghost_project",
    }


def test_api_project_history_treats_slug_like_path_as_truthful_miss(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "slug-like-history.db")
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
        response = client.get("/api/projects/alpha-project/history")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:alpha-project",
    }


def test_api_project_history_does_not_break_projects_status_health_or_ready(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    db_path = tmp_path / "history-non-regression.db"
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
    app.state.state_db.record_task(
        _summary(
            task_id="task-alpha-1",
            branch="feature/task-alpha-1",
            finished_at=1000.0,
            project_id="alpha_project",
        )
    )

    with TestClient(app) as client:
        history_response = client.get("/api/projects/alpha_project/history")
        projects_response = client.get("/api/projects")
        status_response = client.get("/api/projects/alpha_project/status")
        health_response = client.get("/healthz")
        ready_response = client.get("/readyz")

    history_payload = history_response.json()

    assert history_response.status_code == 200
    assert history_payload["project_id"] == "alpha_project"
    assert all("thread_id" not in item for item in history_payload["items"])
    assert all("team" not in item for item in history_payload["items"])
    assert projects_response.status_code == 200
    assert status_response.status_code == 200
    assert health_response.status_code == 200
    assert ready_response.status_code == 200

