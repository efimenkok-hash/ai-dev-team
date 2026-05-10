from pathlib import Path

import pytest

from core.coordinator_onboarding import (
    ProjectCaptainOnboardingContext,
    ProjectCaptainOnboardingService,
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


def test_onboarding_context_happy_path_for_bound_chat(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo, chat_binding=_chat_binding())

    context = ProjectCaptainOnboardingContext(
        snapshot=snapshot,
        chat_provider="telegram",
        chat_id=-100123,
        user_id=101,
        context_source="bound_chat",
        owner_task_text="Build a CLI command.",
    )

    assert context.context_source == "bound_chat"


def test_onboarding_context_happy_path_for_owner_dm_single_project(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo, chat_binding=None)

    context = ProjectCaptainOnboardingContext(
        snapshot=snapshot,
        chat_provider="telegram",
        chat_id=101,
        user_id=101,
        context_source="owner_dm_single_project",
        owner_task_text="Fix the deploy script.",
    )

    assert context.context_source == "owner_dm_single_project"


def test_onboarding_context_rejects_bad_snapshot(tmp_path):
    _git_repo(tmp_path)

    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        ProjectCaptainOnboardingContext(
            snapshot="bad",  # type: ignore[arg-type]
            chat_provider="telegram",
            chat_id=1,
            user_id=1,
            context_source="owner_dm_single_project",
            owner_task_text="Task",
        )


def test_onboarding_context_rejects_snapshot_without_runtime_binding(tmp_path):
    snapshot = _snapshot(None)

    with pytest.raises(ValueError, match="snapshot_missing_runtime_binding"):
        ProjectCaptainOnboardingContext(
            snapshot=snapshot,
            chat_provider="telegram",
            chat_id=1,
            user_id=1,
            context_source="owner_dm_single_project",
            owner_task_text="Task",
        )


@pytest.mark.parametrize("bad", ["", "   ", 123])
def test_onboarding_context_rejects_bad_chat_provider(tmp_path, bad):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="empty_chat_provider"):
        ProjectCaptainOnboardingContext(
            snapshot=snapshot,
            chat_provider=bad,  # type: ignore[arg-type]
            chat_id=1,
            user_id=1,
            context_source="owner_dm_single_project",
            owner_task_text="Task",
        )


@pytest.mark.parametrize("bad", [0, True, False])
def test_onboarding_context_rejects_bad_chat_id(tmp_path, bad):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="invalid_chat_id"):
        ProjectCaptainOnboardingContext(
            snapshot=snapshot,
            chat_provider="telegram",
            chat_id=bad,  # type: ignore[arg-type]
            user_id=1,
            context_source="owner_dm_single_project",
            owner_task_text="Task",
        )


@pytest.mark.parametrize("bad", [0, -1, True, False])
def test_onboarding_context_rejects_bad_user_id(tmp_path, bad):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="invalid_user_id"):
        ProjectCaptainOnboardingContext(
            snapshot=snapshot,
            chat_provider="telegram",
            chat_id=1,
            user_id=bad,  # type: ignore[arg-type]
            context_source="owner_dm_single_project",
            owner_task_text="Task",
        )


@pytest.mark.parametrize("bad", ["none", "registry", "", "   "])
def test_onboarding_context_rejects_bad_context_source(tmp_path, bad):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="invalid_context_source"):
        ProjectCaptainOnboardingContext(
            snapshot=snapshot,
            chat_provider="telegram",
            chat_id=1,
            user_id=1,
            context_source=bad,
            owner_task_text="Task",
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_onboarding_context_rejects_empty_owner_task_text(tmp_path, bad):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="empty_owner_task_text"):
        ProjectCaptainOnboardingContext(
            snapshot=snapshot,
            chat_provider="telegram",
            chat_id=1,
            user_id=1,
            context_source="owner_dm_single_project",
            owner_task_text=bad,
        )


def test_build_pipeline_task_prompt_includes_project_runtime_and_owner_task(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo, chat_binding=_chat_binding())
    context = ProjectCaptainOnboardingContext(
        snapshot=snapshot,
        chat_provider="telegram",
        chat_id=-100123,
        user_id=101,
        context_source="bound_chat",
        owner_task_text="Implement a healthcheck endpoint.",
    )

    prompt = ProjectCaptainOnboardingService().build_pipeline_task_prompt(context)

    assert "project captain" in prompt
    assert "alpha_project" in prompt
    assert "alpha-project" in prompt
    assert str(repo.resolve()) in prompt
    assert str((repo.parent / "worktrees").resolve()) in prompt
    assert "alpha_adapter" in prompt
    assert "main" in prompt
    assert "feature/" in prompt
    assert "python" in prompt
    assert "explicit project chat" in prompt
    assert "Implement a healthcheck endpoint." in prompt


def test_build_pipeline_task_prompt_for_owner_dm_explicitly_marks_fallback(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    context = ProjectCaptainOnboardingContext(
        snapshot=snapshot,
        chat_provider="telegram",
        chat_id=101,
        user_id=101,
        context_source="owner_dm_single_project",
        owner_task_text="Prepare the release branch.",
    )

    prompt = ProjectCaptainOnboardingService().build_pipeline_task_prompt(context)

    assert "owner DM fallback" in prompt
    assert "Prepare the release branch." in prompt
