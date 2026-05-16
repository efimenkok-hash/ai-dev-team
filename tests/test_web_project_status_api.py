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


def test_api_project_status_returns_truthful_persisted_snapshot(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "status.db"))
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
        response = client.get("/api/projects/alpha_project/status")

    assert response.status_code == 200
    assert response.json() == {
        "project": {
            "project_id": "alpha_project",
            "slug": "alpha-project",
            "name": "Alpha Project",
            "description": "Primary project.",
            "status": "active",
            "owner_user_id": 101,
        },
        "bindings": {
            "has_policy": True,
            "has_chat_binding": True,
            "has_runtime_binding": True,
        },
        "policy": {
            "allow_hiring": True,
            "allow_agent_dm": False,
            "require_owner_approval_for_hires": True,
        },
    }


def test_api_project_status_returns_null_policy_when_snapshot_has_no_policy(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "missing-policy.db")
    )
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
            with_runtime_binding=True,
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/projects/beta_project/status")

    assert response.status_code == 200
    assert response.json() == {
        "project": {
            "project_id": "beta_project",
            "slug": "beta-project",
            "name": "Beta Project",
            "description": "Secondary project.",
            "status": "active",
            "owner_user_id": 202,
        },
        "bindings": {
            "has_policy": False,
            "has_chat_binding": False,
            "has_runtime_binding": True,
        },
        "policy": None,
    }


def test_api_project_status_returns_truthful_404_for_unknown_project(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(module.WebAppConfig(state_db_path=tmp_path / "404.db"))

    with TestClient(app) as client:
        response = client.get("/api/projects/ghost_project/status")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:ghost_project",
    }


def test_api_project_status_treats_slug_like_path_as_truthful_miss(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(state_db_path=tmp_path / "slug-like-miss.db")
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
        response = client.get("/api/projects/alpha-project/status")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "unknown_project_id:alpha-project",
    }


def test_api_project_status_does_not_break_projects_health_or_ready_surfaces(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    db_path = tmp_path / "status-non-regression.db"
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

    with TestClient(app) as client:
        status_response = client.get("/api/projects/alpha_project/status")
        projects_response = client.get("/api/projects")
        health_response = client.get("/healthz")
        ready_response = client.get("/readyz")

    status_payload = status_response.json()

    assert status_response.status_code == 200
    assert "team" not in status_payload
    assert "history" not in status_payload
    assert "threads" not in status_payload
    assert projects_response.status_code == 200
    assert projects_response.json() == {
        "items": [
            {
                "project_id": "alpha_project",
                "slug": "alpha-project",
                "name": "Alpha Project",
                "description": "Primary project.",
                "status": "active",
                "owner_user_id": 101,
                "has_policy": True,
                "has_chat_binding": True,
                "has_runtime_binding": False,
            }
        ],
        "count": 1,
    }
    assert health_response.status_code == 200
    assert ready_response.status_code == 200
