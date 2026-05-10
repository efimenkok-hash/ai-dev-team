from pathlib import Path

import pytest

from core.coordinator_owner_escalation import (
    VALID_OWNER_ESCALATION_TYPES,
    CoordinatorOwnerEscalationContext,
    CoordinatorOwnerEscalationService,
    classify_owner_escalation_type,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


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


def _chat_binding(**overrides) -> ProjectChatBinding:
    data = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -100123,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


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


def _snapshot(
    repo_path: Path | None,
    *,
    chat_binding: ProjectChatBinding | None = None,
    **overrides,
) -> ProjectSnapshot:
    data = {
        "project": _project(),
        "policy": _policy(),
        "chat_binding": chat_binding,
    }
    if repo_path is not None:
        data["runtime_binding"] = _runtime_binding(repo_path)
    data.update(overrides)
    return ProjectSnapshot(**data)


def test_context_happy_path_for_bound_chat(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        final_state="BLOCKED",
        failure_reason="writer_blocked:missing architecture",
    )

    assert context.final_state == "BLOCKED"


def test_context_happy_path_for_owner_dm_fallback(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        final_state="FAIL",
        failure_reason="agent_exception:RuntimeError:kaboom",
    )

    assert context.context_source == "owner_dm_single_project"


def test_context_rejects_bad_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        CoordinatorOwnerEscalationContext(
            snapshot="bad",  # type: ignore[arg-type]
            owner_task_text="Task",
            context_source="bound_chat",
            final_state="FAIL",
            failure_reason="agent_exception:RuntimeError:kaboom",
        )


def test_context_rejects_snapshot_without_runtime_binding(tmp_path):
    with pytest.raises(ValueError, match="snapshot_missing_runtime_binding"):
        CoordinatorOwnerEscalationContext(
            snapshot=_snapshot(None),
            owner_task_text="Task",
            context_source="bound_chat",
            final_state="FAIL",
            failure_reason="agent_exception:RuntimeError:kaboom",
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_context_rejects_empty_owner_task_text(tmp_path, bad):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="empty_owner_task_text"):
        CoordinatorOwnerEscalationContext(
            snapshot=_snapshot(repo),
            owner_task_text=bad,
            context_source="bound_chat",
            final_state="FAIL",
            failure_reason="agent_exception:RuntimeError:kaboom",
        )


@pytest.mark.parametrize("bad", ["none", "registry", "", "  "])
def test_context_rejects_bad_context_source(tmp_path, bad):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="invalid_context_source"):
        CoordinatorOwnerEscalationContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source=bad,
            final_state="FAIL",
            failure_reason="agent_exception:RuntimeError:kaboom",
        )


@pytest.mark.parametrize("bad", ["SUCCESS", "CANCELLED", "", "  "])
def test_context_rejects_invalid_final_state(tmp_path, bad):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="invalid_final_state"):
        CoordinatorOwnerEscalationContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source="bound_chat",
            final_state=bad,
            failure_reason="agent_exception:RuntimeError:kaboom",
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_context_rejects_empty_failure_reason(tmp_path, bad):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="empty_failure_reason"):
        CoordinatorOwnerEscalationContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source="bound_chat",
            final_state="FAIL",
            failure_reason=bad,
        )


@pytest.mark.parametrize(
    ("failure_reason", "final_state", "expected_type"),
    [
        ("writer_blocked:missing arch", "BLOCKED", "project_blocked"),
        ("review_fix_loop_exceeded", "FAIL", "quality_repair_exhausted"),
        (
            "runtime_validator_exception:RuntimeError:x",
            "FAIL",
            "runtime_validation_failed",
        ),
        (
            "qa_fix_loop_exceeded:runtime:lint failed",
            "FAIL",
            "runtime_validation_failed",
        ),
        (
            "commit_failed:SandboxError:nothing_to_commit",
            "FAIL",
            "publish_failure",
        ),
        ("agent_exception:RuntimeError:kaboom", "FAIL", "system_failure"),
    ],
)
def test_classification_rules(tmp_path, failure_reason, final_state, expected_type):
    repo = _git_repo(tmp_path)
    context = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        final_state=final_state,
        failure_reason=failure_reason,
    )

    assert expected_type in VALID_OWNER_ESCALATION_TYPES
    assert classify_owner_escalation_type(context) == expected_type
    assert (
        CoordinatorOwnerEscalationService().classify_owner_escalation_type(
            context
        )
        == expected_type
    )


def test_owner_escalation_artifact_contract(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        final_state="FAIL",
        failure_reason="runtime_validator_returned_invalid_report",
    )

    artifact = CoordinatorOwnerEscalationService().build_owner_escalation_artifact(
        context
    )

    assert "Coordinator owner escalation" in artifact
    assert "escalation_type: runtime_validation_failed" in artifact
    assert "final_state: FAIL" in artifact
    assert "failure_reason: runtime_validator_returned_invalid_report" in artifact
    assert "project_id: alpha_project" in artifact
    assert "slug: alpha-project" in artifact
    assert "name: Alpha Project" in artifact
    assert "owner DM fallback" in artifact
    assert "Prepare the release branch." in artifact
    assert "Recommended owner action:" in artifact
    assert "Scope guard:" in artifact
    assert "Do not invent another project or runtime contour." in artifact


def test_owner_escalation_artifact_is_deterministic(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        final_state="BLOCKED",
        failure_reason="writer_blocked:missing arch",
    )
    service = CoordinatorOwnerEscalationService()

    first = service.build_owner_escalation_artifact(context)
    second = service.build_owner_escalation_artifact(context)

    assert first == second


def test_owner_escalation_reply_contract_and_type_specificity(tmp_path):
    repo = _git_repo(tmp_path)
    service = CoordinatorOwnerEscalationService()
    project_blocked = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        final_state="BLOCKED",
        failure_reason="writer_blocked:missing arch",
    )
    publish_failure = CoordinatorOwnerEscalationContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        final_state="FAIL",
        failure_reason="commit_failed:SandboxError:nothing_to_commit",
    )

    blocked_reply = service.build_owner_escalation_reply(project_blocked)
    publish_reply = service.build_owner_escalation_reply(publish_failure)

    assert blocked_reply
    assert publish_reply
    assert blocked_reply != project_blocked.failure_reason
    assert publish_reply != publish_failure.failure_reason
    assert blocked_reply != publish_reply
    assert "заблокирована" in blocked_reply
    assert "publish step" in publish_reply
    assert blocked_reply == service.build_owner_escalation_reply(project_blocked)
    assert publish_reply == service.build_owner_escalation_reply(publish_failure)
