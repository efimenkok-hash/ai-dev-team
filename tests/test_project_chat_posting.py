from pathlib import Path

import pytest

from core.coordinator_role import COORDINATOR_ROLE
from core.progress_emitter import ProgressEvent
from core.project_chat_posting import (
    ProjectChatPostingContext,
    ProjectChatPostingService,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding

_DEFAULT_RUNTIME_BINDING = object()


def _project(**overrides) -> Project:
    data = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides) -> ProjectPolicy:
    data = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _runtime_binding(repo_path: Path, **overrides) -> ProjectRuntimeBinding:
    data = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / "worktrees",
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (),
        "commands": (),
        "forbidden_paths": (),
        "forbidden_tokens": (),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


def _chat_binding(**overrides) -> ProjectChatBinding:
    data = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -100123,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _snapshot(
    tmp_path: Path,
    *,
    chat_binding: ProjectChatBinding | None = None,
    runtime_binding: ProjectRuntimeBinding | object = _DEFAULT_RUNTIME_BINDING,
) -> ProjectSnapshot:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    return ProjectSnapshot(
        project=_project(),
        policy=_policy(),
        chat_binding=chat_binding,
        runtime_binding=(
            _runtime_binding(repo)
            if runtime_binding is _DEFAULT_RUNTIME_BINDING
            else runtime_binding
        ),
    )


def _event(kind: str, **overrides) -> ProgressEvent:
    data = {
        "kind": kind,
        "timestamp": 1.0,
        "detail": "",
    }
    data.update(overrides)
    return ProgressEvent(**data)


def test_project_chat_posting_context_happy_path_bound_chat(tmp_path):
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    assert context.context_source == "bound_chat"


def test_project_chat_posting_context_happy_path_owner_dm_fallback(tmp_path):
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path),
        chat_id=101,
        context_source="owner_dm_single_project",
    )

    assert context.context_source == "owner_dm_single_project"


def test_project_chat_posting_context_rejects_bad_snapshot():
    with pytest.raises(
        ValueError,
        match="invalid_project_snapshot_type:str",
    ):
        ProjectChatPostingContext(  # type: ignore[arg-type]
            snapshot="bad",
            chat_id=1,
            context_source="bound_chat",
        )


def test_project_chat_posting_context_rejects_snapshot_without_runtime_binding(
    tmp_path,
):
    with pytest.raises(ValueError, match="project_snapshot_missing_runtime_binding"):
        ProjectChatPostingContext(
            snapshot=_snapshot(tmp_path, runtime_binding=None),
            chat_id=1,
            context_source="owner_dm_single_project",
        )


@pytest.mark.parametrize("bad_chat_id", [0, True, "1"])
def test_project_chat_posting_context_rejects_bad_chat_id(tmp_path, bad_chat_id):
    with pytest.raises(ValueError, match="invalid_project_chat_id|project_chat_id_zero"):
        ProjectChatPostingContext(
            snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
            chat_id=bad_chat_id,  # type: ignore[arg-type]
            context_source="bound_chat",
        )


def test_project_chat_posting_context_rejects_bad_context_source(tmp_path):
    with pytest.raises(
        ValueError,
        match="invalid_project_chat_posting_context_source",
    ):
        ProjectChatPostingContext(
            snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
            chat_id=-100123,
            context_source="none",
        )


def test_project_chat_posting_context_rejects_bound_chat_without_binding(tmp_path):
    with pytest.raises(ValueError, match="bound_chat_requires_explicit_chat_binding"):
        ProjectChatPostingContext(
            snapshot=_snapshot(tmp_path),
            chat_id=-100123,
            context_source="bound_chat",
        )


def test_bound_chat_agent_started_uses_agent_role(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    assert (
        service.resolve_event_sender_role(
            context,
            _event("agent_started", agent_role="architect_agent"),
        )
        == "architect_agent"
    )


def test_bound_chat_agent_finished_uses_agent_role(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    assert (
        service.resolve_event_sender_role(
            context,
            _event("agent_finished", agent_role="writer_agent", duration_ms=42),
        )
        == "writer_agent"
    )


def test_bound_chat_agent_failed_uses_agent_role(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    assert (
        service.resolve_event_sender_role(
            context,
            _event("agent_failed", agent_role="reviewer_agent", detail="boom"),
        )
        == "reviewer_agent"
    )


def test_bound_chat_task_started_uses_coordinator(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    assert service.resolve_event_sender_role(context, _event("task_started")) == COORDINATOR_ROLE


def test_bound_chat_task_failed_uses_coordinator(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    assert service.resolve_event_sender_role(context, _event("task_failed")) == COORDINATOR_ROLE


def test_owner_dm_fallback_any_event_uses_coordinator(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path),
        chat_id=101,
        context_source="owner_dm_single_project",
    )

    assert (
        service.resolve_event_sender_role(
            context,
            _event("agent_started", agent_role="writer_agent"),
        )
        == COORDINATOR_ROLE
    )


def test_event_envelope_preserves_chat_id_and_formatted_text(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    envelope = service.build_event_envelope(
        context,
        _event("agent_finished", agent_role="writer_agent", duration_ms=321),
    )

    assert envelope.message.chat_id == -100123
    assert envelope.sender_role == "writer_agent"
    assert "writer_agent" in envelope.message.text
    assert "321" in envelope.message.text


def test_system_envelope_always_uses_coordinator(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )

    envelope = service.build_system_envelope(context, "🌳 worktree готов")

    assert envelope.sender_role == COORDINATOR_ROLE
    assert envelope.message.text == "🌳 worktree готов"


def test_terminal_envelope_always_uses_coordinator(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path),
        chat_id=101,
        context_source="owner_dm_single_project",
    )

    envelope = service.build_terminal_envelope(context, "✅ Готово")

    assert envelope.sender_role == COORDINATOR_ROLE
    assert envelope.message.text == "✅ Готово"


def test_project_chat_posting_is_deterministic(tmp_path):
    service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_snapshot(tmp_path, chat_binding=_chat_binding()),
        chat_id=-100123,
        context_source="bound_chat",
    )
    event = _event("agent_started", agent_role="architect_agent")

    first = service.build_event_envelope(context, event)
    second = service.build_event_envelope(context, event)

    assert first == second
