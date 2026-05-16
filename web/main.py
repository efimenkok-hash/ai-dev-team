from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request

from core.coordinator_team_assembly import CoordinatorTeamAssemblyService
from core.coordinator_team_proposal import CoordinatorTeamProposalService
from core.project_registry import ProjectRegistry
from core.state_db import StateDB

DEFAULT_WEB_APP_NAME = "AI Dev Team Web Office API"
DEFAULT_STATE_DB_PATH = Path("~/.ai-dev-team/state.db").expanduser()
DEFAULT_FALLBACK_STATE_DB_PATH = (
    Path(tempfile.gettempdir()) / "ai-dev-team-web" / "state.db"
)


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

    return app


app = create_app()
