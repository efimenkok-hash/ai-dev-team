from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from core.agent_role_catalog import BASELINE_INTERNAL_TEAM_ROLE_ORDER
from core.coordinator_team_assembly import CoordinatorTeamAssemblyService
from core.coordinator_team_proposal import CoordinatorTeamProposalService
from core.hire_approval import PendingHireRequest
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
from core.state_db import StateDB
from core.task_history import TaskSummary

DEFAULT_WEB_APP_NAME = "AI Dev Team Web Office API"
DEFAULT_STATE_DB_PATH = Path("~/.ai-dev-team/state.db").expanduser()
DEFAULT_FALLBACK_STATE_DB_PATH = (
    Path(tempfile.gettempdir()) / "ai-dev-team-web" / "state.db"
)
DEFAULT_PROJECT_HISTORY_LIMIT = 20


def _normalize_state_db_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ValueError(f"invalid_state_db_path_type:{type(path).__name__}")
    expanded = path.expanduser()
    if expanded == Path(".") or not str(expanded).strip():
        raise ValueError("empty_state_db_path")
    return expanded


def _is_default_bootstrap_fallback_error(exc: Exception) -> bool:
    if isinstance(exc, (PermissionError, NotADirectoryError)):
        return True
    if isinstance(exc, OSError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).strip().lower()
        return message in {
            "unable to open database file",
            "attempt to write a readonly database",
        }
    return False


@dataclass(frozen=True)
class WebAppConfig:
    state_db_path: Path
    app_name: str = DEFAULT_WEB_APP_NAME
    debug: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state_db_path",
            _normalize_state_db_path(self.state_db_path),
        )
        if not isinstance(self.app_name, str) or not self.app_name.strip():
            raise ValueError("empty_web_app_name")
        object.__setattr__(self, "app_name", self.app_name.strip())
        if not isinstance(self.debug, bool):
            raise ValueError(f"invalid_web_app_debug_type:{type(self.debug).__name__}")

    @classmethod
    def from_env(cls) -> WebAppConfig:
        state_db_path = os.environ.get("STATE_DB_PATH")
        return cls(
            state_db_path=(
                Path(state_db_path)
                if state_db_path is not None
                else DEFAULT_STATE_DB_PATH
            )
        )


def get_state_db(request: Request) -> StateDB:
    state_db = getattr(request.app.state, "state_db", None)
    if not isinstance(state_db, StateDB):
        raise RuntimeError("web_state_db_unavailable")
    return state_db


def get_project_registry(request: Request) -> ProjectRegistry:
    project_registry = getattr(request.app.state, "project_registry", None)
    if not isinstance(project_registry, ProjectRegistry):
        raise RuntimeError("web_project_registry_unavailable")
    return project_registry


def _get_project_snapshot_or_404(
    registry: ProjectRegistry,
    project_id: str,
) -> ProjectSnapshot:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            "invalid_project_registry_type:"
            f"{type(registry).__name__}"
        )
    try:
        snapshot = registry.get_project_snapshot(project_id)
    except ValueError as exc:
        if str(exc).startswith("invalid_project_id:"):
            raise HTTPException(
                status_code=404,
                detail=f"unknown_project_id:{project_id}",
            ) from exc
        raise
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown_project_id:{project_id}",
        )
    return snapshot


def _serialize_project(snapshot: ProjectSnapshot) -> dict[str, object]:
    if not isinstance(snapshot, ProjectSnapshot):
        raise ValueError(
            "invalid_project_snapshot_type:"
            f"{type(snapshot).__name__}"
        )
    project = snapshot.project
    return {
        "project_id": project.project_id,
        "slug": project.slug,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "owner_user_id": project.owner_user_id,
    }


def _serialize_project_snapshot(snapshot: ProjectSnapshot) -> dict[str, object]:
    project = _serialize_project(snapshot)
    return {
        **project,
        "has_policy": snapshot.policy is not None,
        "has_chat_binding": snapshot.chat_binding is not None,
        "has_runtime_binding": snapshot.runtime_binding is not None,
    }


def _serialize_project_status(snapshot: ProjectSnapshot) -> dict[str, object]:
    policy = snapshot.policy
    return {
        "project": _serialize_project(snapshot),
        "bindings": {
            "has_policy": policy is not None,
            "has_chat_binding": snapshot.chat_binding is not None,
            "has_runtime_binding": snapshot.runtime_binding is not None,
        },
        "policy": (
            {
                "allow_hiring": policy.allow_hiring,
                "allow_agent_dm": policy.allow_agent_dm,
                "require_owner_approval_for_hires": (
                    policy.require_owner_approval_for_hires
                ),
            }
            if policy is not None
            else None
        ),
    }


def _serialize_task_summary(summary: TaskSummary) -> dict[str, object]:
    if not isinstance(summary, TaskSummary):
        raise ValueError(f"invalid_task_summary_type:{type(summary).__name__}")
    return {
        "task_id": summary.task_id,
        "branch": summary.branch,
        "commit_sha": summary.commit_sha,
        "final_state": summary.final_state,
        "failure_reason": summary.failure_reason,
        "tier_name": summary.tier_name,
        "finished_at": float(summary.finished_at),
    }


def _serialize_project_history(
    project_id: str,
    items: list[TaskSummary],
) -> dict[str, object]:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("empty_project_id")
    if not isinstance(items, list):
        raise ValueError(f"invalid_history_items_type:{type(items).__name__}")
    serialized_items = [
        _serialize_task_summary(summary)
        for summary in items
    ]
    return {
        "project_id": project_id,
        "items": serialized_items,
        "count": len(serialized_items),
    }


def _serialize_pending_hire_request(
    request: PendingHireRequest,
) -> dict[str, object]:
    if not isinstance(request, PendingHireRequest):
        raise ValueError(
            "invalid_pending_hire_request_type:"
            f"{type(request).__name__}"
        )
    return {
        "request_id": request.request_id,
        "specialist_role": request.specialist_role,
        "reason": request.reason,
        "source": request.source,
        "created_at": float(request.created_at),
    }


def _serialize_project_team(
    project_id: str,
    roster: ProjectSpecialistRoster,
    pending_requests: tuple[PendingHireRequest, ...],
) -> dict[str, object]:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("empty_project_id")
    if not isinstance(roster, ProjectSpecialistRoster):
        raise ValueError(
            "invalid_project_specialist_roster_type:"
            f"{type(roster).__name__}"
        )
    if roster.project_id != project_id:
        raise ValueError(
            "project_specialist_roster_project_id_mismatch:"
            f"{roster.project_id}!={project_id}"
        )
    if not isinstance(pending_requests, tuple):
        raise ValueError(
            "pending_hire_requests_must_be_tuple"
        )
    serialized_pending_requests = [
        _serialize_pending_hire_request(request)
        for request in pending_requests
    ]
    return {
        "project_id": project_id,
        "baseline_internal_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "project_specialist_roster": list(roster.specialist_roles),
        "resolved_team_roles": list(roster.resolved_team_roles()),
        "pending_hire_requests": serialized_pending_requests,
    }


def create_app(config: WebAppConfig | None = None) -> FastAPI:
    resolved_config = config if config is not None else WebAppConfig.from_env()
    if not isinstance(resolved_config, WebAppConfig):
        raise ValueError(f"invalid_web_app_config_type:{type(resolved_config).__name__}")

    state_db_fallback_in_use = False
    try:
        state_db = StateDB(resolved_config.state_db_path)
    except Exception as exc:
        if (
            config is not None
            or os.environ.get("STATE_DB_PATH") is not None
            or not _is_default_bootstrap_fallback_error(exc)
        ):
            raise
        resolved_config = WebAppConfig(
            state_db_path=DEFAULT_FALLBACK_STATE_DB_PATH,
            app_name=resolved_config.app_name,
            debug=resolved_config.debug,
        )
        state_db = StateDB(resolved_config.state_db_path)
        state_db_fallback_in_use = True
    project_registry = ProjectRegistry(state_db)
    coordinator_team_assembly_service = CoordinatorTeamAssemblyService()
    coordinator_team_proposal_service = CoordinatorTeamProposalService()

    app = FastAPI(
        title=resolved_config.app_name,
        debug=resolved_config.debug,
    )
    app.state.config = resolved_config
    app.state.state_db = state_db
    app.state.state_db_fallback_in_use = state_db_fallback_in_use
    app.state.project_registry = project_registry
    app.state.coordinator_team_assembly_service = coordinator_team_assembly_service
    app.state.coordinator_team_proposal_service = coordinator_team_proposal_service

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        db = get_state_db(request)
        return {
            "ok": True,
            "app": request.app.title,
            "schema_version": db.schema_version(),
            "project_registry_ready": isinstance(registry, ProjectRegistry),
            "state_db_path": str(db.path),
            "state_db_fallback_in_use": bool(
                getattr(request.app.state, "state_db_fallback_in_use", False)
            ),
        }

    @app.get("/readyz")
    def readyz(request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        db = get_state_db(request)
        return {
            "ok": True,
            "ready": True,
            "app": request.app.title,
            "schema_version": db.schema_version(),
            "project_registry_ready": isinstance(registry, ProjectRegistry),
            "state_db_path": str(db.path),
            "state_db_fallback_in_use": bool(
                getattr(request.app.state, "state_db_fallback_in_use", False)
            ),
        }

    @app.get("/api/projects")
    def list_projects(request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        items = [
            _serialize_project_snapshot(snapshot)
            for snapshot in sorted(
                registry.list_project_snapshots(),
                key=lambda snapshot: snapshot.project.project_id,
            )
        ]
        return {
            "items": items,
            "count": len(items),
        }

    @app.get("/api/projects/{project_id}/status")
    def project_status(project_id: str, request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        snapshot = _get_project_snapshot_or_404(registry, project_id)
        return _serialize_project_status(snapshot)

    @app.get("/api/projects/{project_id}/history")
    def project_history(project_id: str, request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        snapshot = _get_project_snapshot_or_404(registry, project_id)
        items = registry.list_project_task_history(
            snapshot.project.project_id,
            limit=DEFAULT_PROJECT_HISTORY_LIMIT,
        )
        return _serialize_project_history(snapshot.project.project_id, items)

    @app.get("/api/projects/{project_id}/team")
    def project_team(project_id: str, request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        snapshot = _get_project_snapshot_or_404(registry, project_id)
        normalized_project_id = snapshot.project.project_id
        roster = registry.get_project_specialist_roster(normalized_project_id)
        pending_requests = registry.list_pending_hire_requests(normalized_project_id)
        return _serialize_project_team(
            normalized_project_id,
            roster,
            pending_requests,
        )

    return app


app = create_app()
