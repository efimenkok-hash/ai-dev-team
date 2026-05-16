from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from core.agent_bus_models import ProjectThread
from core.hire_approval import PendingHireRequest
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
from core.task_history import TaskSummary

PROJECT_EVENT_SURFACE_ORDER: tuple[str, ...] = (
    "status",
    "history",
    "team",
    "threads",
)
_PROJECT_EVENT_SURFACE_INDEX = {
    surface: index for index, surface in enumerate(PROJECT_EVENT_SURFACE_ORDER)
}
_PROJECT_EVENT_TYPES = frozenset({"hello", "invalidate", "error"})


def _normalize_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("missing_project_id")
    normalized = project_id.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"unknown_project_id:{normalized}")
    if not normalized[0].isalpha():
        raise ValueError(f"unknown_project_id:{normalized}")
    for char in normalized:
        if not (char.islower() or char.isdigit() or char == "_"):
            raise ValueError(f"unknown_project_id:{normalized}")
    if len(normalized) > 64:
        raise ValueError(f"unknown_project_id:{normalized}")
    return normalized


def resolve_project_snapshot(
    registry: ProjectRegistry,
    project_id: str | None,
) -> ProjectSnapshot:
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            "invalid_project_registry_type:"
            f"{type(registry).__name__}"
        )
    normalized_project_id = _normalize_project_id("" if project_id is None else project_id)
    snapshot = registry.get_project_snapshot(normalized_project_id)
    if snapshot is None:
        raise ValueError(f"unknown_project_id:{normalized_project_id}")
    return snapshot


@dataclass(frozen=True)
class ProjectEventsStreamConfig:
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.poll_interval_seconds, bool)
            or not isinstance(self.poll_interval_seconds, (int, float))
        ):
            raise ValueError(
                "invalid_project_events_poll_interval_seconds:"
                f"{self.poll_interval_seconds!r}"
            )
        normalized = float(self.poll_interval_seconds)
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError(
                "invalid_project_events_poll_interval_seconds:"
                f"{self.poll_interval_seconds!r}"
            )
        object.__setattr__(self, "poll_interval_seconds", normalized)


@dataclass(frozen=True)
class ProjectSurfaceSignature:
    project_id: str
    status_signature: tuple[object, ...]
    history_signature: tuple[object, ...]
    team_signature: tuple[object, ...]
    threads_signature: tuple[object, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _normalize_project_id(self.project_id))
        for field_name in (
            "status_signature",
            "history_signature",
            "team_signature",
            "threads_signature",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise ValueError(f"{field_name}_must_be_tuple")


@dataclass(frozen=True)
class ProjectEventEnvelope:
    type: str
    project_id: str
    emitted_at: float
    surfaces: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or self.type not in _PROJECT_EVENT_TYPES:
            raise ValueError(f"invalid_project_event_type:{self.type!r}")
        if not isinstance(self.project_id, str):
            raise ValueError(
                "invalid_project_event_project_id_type:"
                f"{type(self.project_id).__name__}"
            )
        object.__setattr__(self, "project_id", self.project_id.strip())
        if (
            isinstance(self.emitted_at, bool)
            or not isinstance(self.emitted_at, (int, float))
        ):
            raise ValueError(f"invalid_project_event_emitted_at:{self.emitted_at!r}")
        normalized_emitted_at = float(self.emitted_at)
        if not math.isfinite(normalized_emitted_at) or normalized_emitted_at <= 0:
            raise ValueError(f"invalid_project_event_emitted_at:{self.emitted_at!r}")
        object.__setattr__(self, "emitted_at", normalized_emitted_at)
        if not isinstance(self.surfaces, tuple):
            raise ValueError("project_event_surfaces_must_be_tuple")
        normalized_surfaces: list[str] = []
        seen_surfaces: set[str] = set()
        for surface in self.surfaces:
            if not isinstance(surface, str) or surface not in _PROJECT_EVENT_SURFACE_INDEX:
                raise ValueError(f"invalid_project_event_surface:{surface!r}")
            if surface in seen_surfaces:
                raise ValueError(f"duplicate_project_event_surface:{surface}")
            seen_surfaces.add(surface)
            normalized_surfaces.append(surface)
        normalized_surfaces.sort(key=lambda surface: _PROJECT_EVENT_SURFACE_INDEX[surface])
        object.__setattr__(self, "surfaces", tuple(normalized_surfaces))

        if self.type == "hello":
            if self.project_id == "":
                raise ValueError("hello_event_requires_project_id")
            if self.surfaces != PROJECT_EVENT_SURFACE_ORDER:
                raise ValueError("hello_event_requires_all_project_surfaces")
            if self.detail is not None:
                raise ValueError("hello_event_detail_must_be_none")
            return
        if self.type == "invalidate":
            if self.project_id == "":
                raise ValueError("invalidate_event_requires_project_id")
            if not self.surfaces:
                raise ValueError("invalidate_event_requires_surfaces")
            if self.detail is not None:
                raise ValueError("invalidate_event_detail_must_be_none")
            return
        if self.type == "error":
            if self.surfaces != ():
                raise ValueError("error_event_surfaces_must_be_empty")
            if not isinstance(self.detail, str) or not self.detail.strip():
                raise ValueError("error_event_requires_detail")
            object.__setattr__(self, "detail", self.detail.strip())

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "project_id": self.project_id,
            "emitted_at": self.emitted_at,
            "surfaces": list(self.surfaces),
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def _build_status_signature(snapshot: ProjectSnapshot) -> tuple[object, ...]:
    project = snapshot.project
    policy = snapshot.policy
    return (
        project.project_id,
        project.slug,
        project.name,
        project.description,
        project.status,
        project.owner_user_id,
        policy is not None,
        snapshot.chat_binding is not None,
        snapshot.runtime_binding is not None,
        (
            None
            if policy is None
            else (
                policy.allow_hiring,
                policy.allow_agent_dm,
                policy.require_owner_approval_for_hires,
            )
        ),
    )


def _build_history_signature(
    items: list[TaskSummary],
) -> tuple[object, ...]:
    return tuple(
        (
            item.task_id,
            item.branch,
            item.commit_sha,
            item.final_state,
            item.failure_reason,
            item.tier_name,
            float(item.finished_at),
        )
        for item in items
    )


def _build_team_signature(
    roster: ProjectSpecialistRoster,
    pending_requests: tuple[PendingHireRequest, ...],
) -> tuple[object, ...]:
    return (
        tuple(roster.specialist_roles),
        tuple(
            (
                request.request_id,
                request.specialist_role,
                request.reason,
                request.source,
                float(request.created_at),
            )
            for request in pending_requests
        ),
    )


def _build_threads_signature(
    items: tuple[ProjectThread, ...],
) -> tuple[object, ...]:
    return tuple(
        (
            item.thread_id,
            item.opened_by_role,
            item.status,
            float(item.created_at),
            float(item.last_message_at),
            item.task_id,
        )
        for item in items
    )


def collect_project_surface_signature(
    registry: ProjectRegistry,
    project_id: str | None,
    *,
    history_limit: int = 20,
) -> ProjectSurfaceSignature:
    snapshot = resolve_project_snapshot(registry, project_id)
    normalized_project_id = snapshot.project.project_id
    history_items = registry.list_project_task_history(
        normalized_project_id,
        limit=history_limit,
    )
    roster = registry.get_project_specialist_roster(normalized_project_id)
    pending_requests = registry.list_pending_hire_requests(normalized_project_id)
    threads = registry.list_project_threads(normalized_project_id)
    return ProjectSurfaceSignature(
        project_id=normalized_project_id,
        status_signature=_build_status_signature(snapshot),
        history_signature=_build_history_signature(history_items),
        team_signature=_build_team_signature(roster, pending_requests),
        threads_signature=_build_threads_signature(threads),
    )


def diff_project_surface_signatures(
    previous: ProjectSurfaceSignature,
    current: ProjectSurfaceSignature,
) -> tuple[str, ...]:
    if not isinstance(previous, ProjectSurfaceSignature):
        raise ValueError(
            "invalid_previous_project_surface_signature_type:"
            f"{type(previous).__name__}"
        )
    if not isinstance(current, ProjectSurfaceSignature):
        raise ValueError(
            "invalid_current_project_surface_signature_type:"
            f"{type(current).__name__}"
        )
    if previous.project_id != current.project_id:
        raise ValueError(
            "project_surface_signature_project_id_mismatch:"
            f"{previous.project_id}!={current.project_id}"
        )
    changed_surfaces: list[str] = []
    if previous.status_signature != current.status_signature:
        changed_surfaces.append("status")
    if previous.history_signature != current.history_signature:
        changed_surfaces.append("history")
    if previous.team_signature != current.team_signature:
        changed_surfaces.append("team")
    if previous.threads_signature != current.threads_signature:
        changed_surfaces.append("threads")
    return tuple(changed_surfaces)


def _build_error_envelope(
    *,
    project_id: str | None,
    detail: str,
) -> ProjectEventEnvelope:
    return ProjectEventEnvelope(
        type="error",
        project_id="" if project_id is None else str(project_id).strip(),
        emitted_at=time.time(),
        surfaces=(),
        detail=detail,
    )


async def _send_error_and_close(
    websocket: WebSocket,
    *,
    project_id: str | None,
    detail: str,
) -> None:
    envelope = _build_error_envelope(
        project_id=project_id,
        detail=detail,
    )
    await websocket.send_json(envelope.to_payload())
    await websocket.close(code=1008)


async def stream_project_events(
    websocket: WebSocket,
    *,
    registry: ProjectRegistry,
    config: ProjectEventsStreamConfig,
) -> None:
    if not isinstance(websocket, WebSocket):
        raise ValueError(
            f"invalid_websocket_type:{type(websocket).__name__}"
        )
    if not isinstance(registry, ProjectRegistry):
        raise ValueError(
            "invalid_project_registry_type:"
            f"{type(registry).__name__}"
        )
    if not isinstance(config, ProjectEventsStreamConfig):
        raise ValueError(
            "invalid_project_events_stream_config_type:"
            f"{type(config).__name__}"
        )

    await websocket.accept()
    raw_project_id = websocket.query_params.get("project_id")
    try:
        previous_signature = collect_project_surface_signature(
            registry,
            raw_project_id,
        )
    except ValueError as exc:
        await _send_error_and_close(
            websocket,
            project_id=raw_project_id,
            detail=str(exc),
        )
        return

    hello = ProjectEventEnvelope(
        type="hello",
        project_id=previous_signature.project_id,
        emitted_at=time.time(),
        surfaces=PROJECT_EVENT_SURFACE_ORDER,
    )
    await websocket.send_json(hello.to_payload())

    while True:
        try:
            await asyncio.wait_for(
                websocket.receive_text(),
                timeout=config.poll_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
        except WebSocketDisconnect:
            return

        try:
            current_signature = collect_project_surface_signature(
                registry,
                previous_signature.project_id,
            )
        except ValueError as exc:
            await _send_error_and_close(
                websocket,
                project_id=previous_signature.project_id,
                detail=str(exc),
            )
            return

        changed_surfaces = diff_project_surface_signatures(
            previous_signature,
            current_signature,
        )
        if not changed_surfaces:
            continue

        invalidate = ProjectEventEnvelope(
            type="invalidate",
            project_id=current_signature.project_id,
            emitted_at=time.time(),
            surfaces=changed_surfaces,
        )
        try:
            await websocket.send_json(invalidate.to_payload())
        except WebSocketDisconnect:
            return
        previous_signature = current_signature
