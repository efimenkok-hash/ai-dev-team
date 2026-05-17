from __future__ import annotations

import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.agent_bus_models import ProjectThread
from core.agent_role_catalog import BASELINE_INTERNAL_TEAM_ROLE_ORDER
from core.coordinator_team_assembly import CoordinatorTeamAssemblyService
from core.coordinator_team_proposal import CoordinatorTeamProposalService
from core.hire_approval import PendingHireRequest
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
from core.state_db import StateDB
from core.task_history import TaskSummary
from web.project_events import (
    ProjectEventsStreamConfig,
    resolve_project_snapshot,
    stream_project_events,
)

DEFAULT_WEB_APP_NAME = "AI Dev Team Web Office API"
DEFAULT_STATE_DB_PATH = Path("~/.ai-dev-team/state.db").expanduser()
DEFAULT_FALLBACK_STATE_DB_PATH = (
    Path(tempfile.gettempdir()) / "ai-dev-team-web" / "state.db"
)
DEFAULT_PROJECT_HISTORY_LIMIT = 20
DEFAULT_PROJECT_OVERVIEW_PREVIEW_LIMIT = 3
WEB_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_ROOT / "templates"
STATIC_DIR = WEB_ROOT / "static"


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
    project_events_poll_interval_seconds: float = 1.0

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
        if (
            isinstance(self.project_events_poll_interval_seconds, bool)
            or not isinstance(
                self.project_events_poll_interval_seconds,
                (int, float),
            )
        ):
            raise ValueError(
                "invalid_project_events_poll_interval_seconds:"
                f"{self.project_events_poll_interval_seconds!r}"
            )
        normalized_poll_interval = float(self.project_events_poll_interval_seconds)
        if not math.isfinite(normalized_poll_interval) or normalized_poll_interval <= 0:
            raise ValueError(
                "invalid_project_events_poll_interval_seconds:"
                f"{self.project_events_poll_interval_seconds!r}"
            )
        object.__setattr__(
            self,
            "project_events_poll_interval_seconds",
            normalized_poll_interval,
        )

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
    try:
        snapshot = resolve_project_snapshot(registry, project_id)
    except ValueError as exc:
        if str(exc).startswith("unknown_project_id:") or str(exc) == "missing_project_id":
            raise HTTPException(
                status_code=404,
                detail=f"unknown_project_id:{project_id}",
            ) from exc
        raise
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


def _serialize_project_thread(thread: ProjectThread) -> dict[str, object]:
    if not isinstance(thread, ProjectThread):
        raise ValueError(f"invalid_project_thread_type:{type(thread).__name__}")
    return {
        "thread_id": thread.thread_id,
        "opened_by_role": thread.opened_by_role,
        "status": thread.status,
        "created_at": float(thread.created_at),
        "last_message_at": float(thread.last_message_at),
        "task_id": thread.task_id,
    }


def _serialize_project_threads(
    project_id: str,
    items: tuple[ProjectThread, ...],
) -> dict[str, object]:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("empty_project_id")
    if not isinstance(items, tuple):
        raise ValueError(
            f"invalid_project_threads_type:{type(items).__name__}"
        )
    serialized_items = [
        _serialize_project_thread(thread)
        for thread in items
    ]
    return {
        "project_id": project_id,
        "items": serialized_items,
        "count": len(serialized_items),
    }


def _collect_dashboard_projects(
    registry: ProjectRegistry,
) -> list[dict[str, object]]:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            f"invalid_project_registry_type:{type(registry).__name__}"
        )
    return [
        _serialize_project_snapshot(snapshot)
        for snapshot in sorted(
            registry.list_project_snapshots(),
            key=lambda snapshot: snapshot.project.project_id,
        )
    ]


def _serialize_dashboard_context(
    projects: list[dict[str, object]],
) -> dict[str, object]:
    if not isinstance(projects, list):
        raise ValueError(
            f"invalid_dashboard_projects_type:{type(projects).__name__}"
        )
    total_projects = len(projects)
    active_projects = sum(
        1
        for project in projects
        if project.get("status") == "active"
    )
    runtime_bound_projects = sum(
        1
        for project in projects
        if project.get("has_runtime_binding") is True
    )
    return {
        "projects": projects,
        "metrics": {
            "total_projects": total_projects,
            "active_projects": active_projects,
            "runtime_bound_projects": runtime_bound_projects,
        },
    }


def _serialize_project_view_context(
    registry: ProjectRegistry,
    project_id: str,
) -> dict[str, object]:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            f"invalid_project_registry_type:{type(registry).__name__}"
        )
    snapshot = _get_project_snapshot_or_404(registry, project_id)
    normalized_project_id = snapshot.project.project_id
    status_payload = _serialize_project_status(snapshot)
    roster = registry.get_project_specialist_roster(normalized_project_id)
    pending_requests = registry.list_pending_hire_requests(normalized_project_id)
    history_items = registry.list_project_task_history(
        normalized_project_id,
        limit=DEFAULT_PROJECT_HISTORY_LIMIT,
    )
    thread_items = registry.list_project_threads(normalized_project_id)

    history_preview = [
        _serialize_task_summary(summary)
        for summary in history_items[:DEFAULT_PROJECT_OVERVIEW_PREVIEW_LIMIT]
    ]
    thread_preview = [
        _serialize_project_thread(thread)
        for thread in thread_items[:DEFAULT_PROJECT_OVERVIEW_PREVIEW_LIMIT]
    ]
    pending_preview = [
        _serialize_pending_hire_request(request)
        for request in pending_requests[:DEFAULT_PROJECT_OVERVIEW_PREVIEW_LIMIT]
    ]

    return {
        "project": status_payload["project"],
        "bindings": status_payload["bindings"],
        "policy": status_payload["policy"],
        "team_summary": {
            "approved_specialist_count": len(roster.specialist_roles),
            "pending_hire_request_count": len(pending_requests),
            "resolved_team_size": len(roster.resolved_team_roles()),
            "approved_specialists_preview": list(
                roster.specialist_roles[:DEFAULT_PROJECT_OVERVIEW_PREVIEW_LIMIT]
            ),
            "pending_hire_requests_preview": pending_preview,
        },
        "history_summary": {
            "recent_task_count": len(history_items),
            "latest_task": history_preview[0] if history_preview else None,
            "preview_items": history_preview,
        },
        "threads_summary": {
            "thread_count": len(thread_items),
            "latest_thread": thread_preview[0] if thread_preview else None,
            "preview_items": thread_preview,
        },
    }


def _serialize_project_team_view_context(
    registry: ProjectRegistry,
    project_id: str,
) -> dict[str, object]:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            f"invalid_project_registry_type:{type(registry).__name__}"
        )
    snapshot = _get_project_snapshot_or_404(registry, project_id)
    normalized_project_id = snapshot.project.project_id
    roster = registry.get_project_specialist_roster(normalized_project_id)
    pending_requests = registry.list_pending_hire_requests(normalized_project_id)

    return {
        "project": _serialize_project(snapshot),
        "team_summary": {
            "baseline_internal_team_size": len(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
            "approved_specialist_count": len(roster.specialist_roles),
            "pending_hire_request_count": len(pending_requests),
            "resolved_team_size": len(roster.resolved_team_roles()),
        },
        "baseline_internal_team_roles": list(BASELINE_INTERNAL_TEAM_ROLE_ORDER),
        "approved_specialists": list(roster.specialist_roles),
        "pending_hire_requests": [
            _serialize_pending_hire_request(request)
            for request in pending_requests
        ],
        "resolved_team_roles": list(roster.resolved_team_roles()),
    }


def _serialize_project_history_view_context(
    registry: ProjectRegistry,
    project_id: str,
) -> dict[str, object]:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            f"invalid_project_registry_type:{type(registry).__name__}"
        )
    snapshot = _get_project_snapshot_or_404(registry, project_id)
    normalized_project_id = snapshot.project.project_id
    history_items = registry.list_project_task_history(
        normalized_project_id,
        limit=DEFAULT_PROJECT_HISTORY_LIMIT,
    )
    serialized_items = [
        _serialize_task_summary(summary)
        for summary in history_items
    ]
    latest_task = serialized_items[0] if serialized_items else None
    return {
        "project": _serialize_project(snapshot),
        "history_summary": {
            "recent_task_count": len(serialized_items),
            "latest_task": latest_task,
            "latest_final_state": (
                latest_task["final_state"]
                if latest_task is not None
                else None
            ),
            "latest_branch": (
                latest_task["branch"]
                if latest_task is not None
                else None
            ),
            "latest_finished_at": (
                latest_task["finished_at"]
                if latest_task is not None
                else None
            ),
        },
        "history_items": serialized_items,
    }


def _serialize_project_settings_view_context(
    registry: ProjectRegistry,
    project_id: str,
) -> dict[str, object]:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            f"invalid_project_registry_type:{type(registry).__name__}"
        )
    snapshot = _get_project_snapshot_or_404(registry, project_id)
    policy = snapshot.policy
    chat_binding = snapshot.chat_binding
    runtime_binding = snapshot.runtime_binding
    return {
        "project": _serialize_project(snapshot),
        "bindings": {
            "has_policy": policy is not None,
            "has_chat_binding": chat_binding is not None,
            "has_runtime_binding": runtime_binding is not None,
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
        "chat_binding": (
            {
                "chat_provider": chat_binding.chat_provider,
                "chat_id": chat_binding.chat_id,
            }
            if chat_binding is not None
            else None
        ),
        "runtime_binding": (
            {
                "adapter_name": runtime_binding.adapter_name,
                "base_branch": runtime_binding.base_branch,
                "branch_prefix": runtime_binding.branch_prefix,
                "language": runtime_binding.language,
            }
            if runtime_binding is not None
            else None
        ),
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
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI(
        title=resolved_config.app_name,
        debug=resolved_config.debug,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.config = resolved_config
    app.state.state_db = state_db
    app.state.state_db_fallback_in_use = state_db_fallback_in_use
    app.state.project_registry = project_registry
    app.state.templates = templates
    app.state.project_events_stream_config = ProjectEventsStreamConfig(
        poll_interval_seconds=resolved_config.project_events_poll_interval_seconds
    )
    app.state.coordinator_team_assembly_service = coordinator_team_assembly_service
    app.state.coordinator_team_proposal_service = coordinator_team_proposal_service

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        registry = get_project_registry(request)
        context = _serialize_dashboard_context(_collect_dashboard_projects(registry))
        return templates.TemplateResponse(
            request,
            name="dashboard.html",
            context={
                "request": request,
                **context,
            },
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_view(project_id: str, request: Request):
        registry = get_project_registry(request)
        context = _serialize_project_view_context(registry, project_id)
        return templates.TemplateResponse(
            request,
            name="project.html",
            context={
                "request": request,
                **context,
            },
        )

    @app.get("/projects/{project_id}/team", response_class=HTMLResponse)
    def project_team_view(project_id: str, request: Request):
        registry = get_project_registry(request)
        context = _serialize_project_team_view_context(registry, project_id)
        return templates.TemplateResponse(
            request,
            name="project_team.html",
            context={
                "request": request,
                **context,
            },
        )

    @app.get("/projects/{project_id}/history", response_class=HTMLResponse)
    def project_history_view(project_id: str, request: Request):
        registry = get_project_registry(request)
        context = _serialize_project_history_view_context(registry, project_id)
        return templates.TemplateResponse(
            request,
            name="project_history.html",
            context={
                "request": request,
                **context,
            },
        )

    @app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
    def project_settings_view(project_id: str, request: Request):
        registry = get_project_registry(request)
        context = _serialize_project_settings_view_context(registry, project_id)
        return templates.TemplateResponse(
            request,
            name="project_settings.html",
            context={
                "request": request,
                **context,
            },
        )

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
        items = _collect_dashboard_projects(registry)
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

    @app.get("/api/projects/{project_id}/threads")
    def project_threads(project_id: str, request: Request) -> dict[str, object]:
        registry = get_project_registry(request)
        snapshot = _get_project_snapshot_or_404(registry, project_id)
        normalized_project_id = snapshot.project.project_id
        items = registry.list_project_threads(normalized_project_id)
        return _serialize_project_threads(normalized_project_id, items)

    @app.websocket("/ws/events")
    async def websocket_project_events(websocket: WebSocket) -> None:
        registry = getattr(websocket.app.state, "project_registry", None)
        if not isinstance(registry, ProjectRegistry):
            raise RuntimeError("web_project_registry_unavailable")
        stream_config = getattr(
            websocket.app.state,
            "project_events_stream_config",
            None,
        )
        if not isinstance(stream_config, ProjectEventsStreamConfig):
            raise RuntimeError("web_project_events_stream_config_unavailable")
        await stream_project_events(
            websocket,
            registry=registry,
            config=stream_config,
        )

    return app


app = create_app()
