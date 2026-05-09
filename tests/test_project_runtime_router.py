import shutil

import pytest

from core.project_bootstrap import ProjectBootstrapResult
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_runtime_router import (
    ProjectRuntimeRouter,
    ResolvedProjectRuntime,
    describe_project_runtime_error,
)
from core.sandbox_workspace import SandboxWorkspace
from core.state_db import StateDB
from core.telegram_bridge import IncomingMessage


def _git_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _project(**overrides):
    data = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides):
    data = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _runtime_binding(repo_path, **overrides):
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


def _snapshot(repo_path=None, **overrides):
    data = {
        "project": _project(),
        "policy": _policy(),
    }
    if repo_path is not None:
        data["runtime_binding"] = _runtime_binding(repo_path)
    data.update(overrides)
    return ProjectSnapshot(**data)


def _msg(**overrides):
    data = {
        "chat_id": 1,
        "user_id": 1,
        "message_id": 1,
        "text": "task",
    }
    data.update(overrides)
    return IncomingMessage(**data)


def _bootstrap(snapshot=None, **overrides):
    data = {
        "registry": None,
        "active_snapshot": snapshot,
        "source": "legacy_env_ephemeral" if snapshot is not None else "none",
        "reason": None if snapshot is not None else "bootstrap_unavailable",
    }
    data.update(overrides)
    return ProjectBootstrapResult(**data)


def _db(tmp_path):
    return StateDB(tmp_path / "state.db")


def _register_snapshot(registry, snapshot):
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


def test_describe_project_runtime_error_known_code():
    text = describe_project_runtime_error("message_project_not_found")

    assert "не найден" in text.lower()


def test_describe_project_runtime_error_rejects_empty_code():
    with pytest.raises(ValueError, match="invalid_project_runtime_error_code"):
        describe_project_runtime_error("")


def test_resolved_project_runtime_happy_path(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    runtime_binding = snapshot.runtime_binding
    assert runtime_binding is not None
    sandbox = SandboxWorkspace(runtime_binding.build_sandbox_config())

    resolved = ResolvedProjectRuntime(
        snapshot=snapshot,
        runtime_binding=runtime_binding,
        sandbox=sandbox,
        source="message_project_id",
    )

    assert resolved.snapshot == snapshot


def test_resolved_project_runtime_rejects_bad_snapshot(tmp_path):
    repo = _git_repo(tmp_path)
    binding = _runtime_binding(repo)
    sandbox = SandboxWorkspace(binding.build_sandbox_config())

    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        ResolvedProjectRuntime(
            snapshot="bad",  # type: ignore[arg-type]
            runtime_binding=binding,
            sandbox=sandbox,
            source="message_project_id",
        )


def test_resolved_project_runtime_rejects_bad_runtime_binding(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    sandbox = SandboxWorkspace(snapshot.runtime_binding.build_sandbox_config())  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="invalid_project_runtime_binding_type"):
        ResolvedProjectRuntime(
            snapshot=snapshot,
            runtime_binding="bad",  # type: ignore[arg-type]
            sandbox=sandbox,
            source="message_project_id",
        )


def test_resolved_project_runtime_rejects_bad_sandbox(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    binding = snapshot.runtime_binding
    assert binding is not None

    with pytest.raises(ValueError, match="invalid_sandbox_workspace_type"):
        ResolvedProjectRuntime(
            snapshot=snapshot,
            runtime_binding=binding,
            sandbox="bad",  # type: ignore[arg-type]
            source="message_project_id",
        )


def test_resolved_project_runtime_rejects_bad_source(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    binding = snapshot.runtime_binding
    assert binding is not None
    sandbox = SandboxWorkspace(binding.build_sandbox_config())

    with pytest.raises(ValueError, match="invalid_resolved_project_runtime_source"):
        ResolvedProjectRuntime(
            snapshot=snapshot,
            runtime_binding=binding,
            sandbox=sandbox,
            source="registry",
        )


def test_resolved_project_runtime_rejects_project_mismatch(tmp_path):
    alpha_repo = _git_repo(tmp_path, "alpha")
    beta_repo = _git_repo(tmp_path, "beta")
    snapshot = _snapshot(alpha_repo)
    sandbox = SandboxWorkspace(snapshot.runtime_binding.build_sandbox_config())  # type: ignore[union-attr]
    beta_binding = _runtime_binding(
        beta_repo,
        project_id="beta_project",
        adapter_name="beta_adapter",
    )

    with pytest.raises(ValueError, match="runtime_binding_project_id_mismatch"):
        ResolvedProjectRuntime(
            snapshot=snapshot,
            runtime_binding=beta_binding,
            sandbox=sandbox,
            source="message_project_id",
        )


def test_resolved_project_runtime_rejects_snapshot_without_runtime_binding(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo_path=None)
    sandbox = SandboxWorkspace(_runtime_binding(repo).build_sandbox_config())

    with pytest.raises(ValueError, match="snapshot_missing_runtime_binding"):
        ResolvedProjectRuntime(
            snapshot=snapshot,
            runtime_binding=_runtime_binding(repo),
            sandbox=sandbox,
            source="message_project_id",
        )


def test_project_runtime_router_rejects_bad_registry():
    with pytest.raises(ValueError, match="invalid_project_registry_type"):
        ProjectRuntimeRouter("bad", None)  # type: ignore[arg-type]


def test_project_runtime_router_rejects_bad_bootstrap_result():
    with pytest.raises(ValueError, match="invalid_project_bootstrap_result_type"):
        ProjectRuntimeRouter(None, "bad")  # type: ignore[arg-type]


def test_resolve_message_runtime_uses_exact_message_project_id(tmp_path):
    repo = _git_repo(tmp_path)
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    snapshot = _register_snapshot(registry, _snapshot(repo))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))

    resolved = router.resolve_message_runtime(
        _msg(project_id=snapshot.project.project_id)
    )

    assert resolved.source == "message_project_id"
    assert resolved.snapshot.project.project_id == snapshot.project.project_id
    assert resolved.sandbox.config.main_repo_path == repo.resolve()


def test_resolve_message_runtime_rejects_message_project_without_registry(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    router = ProjectRuntimeRouter(None, _bootstrap(_snapshot(repo)))

    with pytest.raises(ValueError, match="message_project_registry_unavailable"):
        router.resolve_message_runtime(_msg(project_id="alpha_project"))


def test_resolve_message_runtime_rejects_unknown_message_project(tmp_path):
    repo = _git_repo(tmp_path)
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry, _snapshot(repo))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))

    with pytest.raises(ValueError, match="message_project_not_found"):
        router.resolve_message_runtime(_msg(project_id="missing_project"))


def test_resolve_message_runtime_rejects_message_project_without_runtime_binding(
    tmp_path,
):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry, _snapshot(repo_path=None))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))

    with pytest.raises(ValueError, match="message_project_missing_runtime_binding"):
        router.resolve_message_runtime(_msg(project_id="alpha_project"))


def test_resolve_message_runtime_rejects_invalid_message_project_runtime(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    snapshot = _register_snapshot(registry, _snapshot(repo))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))
    shutil.rmtree(snapshot.runtime_binding.repo_path)  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="message_project_runtime_invalid"):
        router.resolve_message_runtime(_msg(project_id="alpha_project"))


def test_resolve_message_runtime_falls_back_to_bootstrap_active_project(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    router = ProjectRuntimeRouter(None, _bootstrap(snapshot))

    resolved = router.resolve_message_runtime(_msg())

    assert resolved.source == "bootstrap_active_project"
    assert resolved.snapshot.project.project_id == "alpha_project"


def test_resolve_message_runtime_rejects_missing_bootstrap_active_project():
    router = ProjectRuntimeRouter(None, _bootstrap(None))

    with pytest.raises(ValueError, match="bootstrap_active_project_unavailable"):
        router.resolve_message_runtime(_msg())


def test_resolve_message_runtime_rejects_bootstrap_snapshot_without_runtime_binding():
    router = ProjectRuntimeRouter(None, _bootstrap(_snapshot(repo_path=None)))

    with pytest.raises(
        ValueError,
        match="bootstrap_active_project_missing_runtime_binding",
    ):
        router.resolve_message_runtime(_msg())


def test_resolve_message_runtime_rejects_invalid_bootstrap_runtime(tmp_path):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo)
    router = ProjectRuntimeRouter(None, _bootstrap(snapshot))
    shutil.rmtree(repo)

    with pytest.raises(
        ValueError,
        match="bootstrap_active_project_runtime_invalid",
    ):
        router.resolve_message_runtime(_msg())


def test_has_any_routable_runtime_true_for_bootstrap_active_runtime(tmp_path):
    repo = _git_repo(tmp_path)
    router = ProjectRuntimeRouter(None, _bootstrap(_snapshot(repo)))

    assert router.has_any_routable_runtime() is True


def test_has_any_routable_runtime_true_for_registry_with_runtime_bound_project(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry, _snapshot(repo))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))

    assert router.has_any_routable_runtime() is True


def test_has_any_routable_runtime_false_for_registry_without_runtime_binding(
    tmp_path,
):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry, _snapshot(repo_path=None))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))

    assert router.has_any_routable_runtime() is False


def test_has_any_routable_runtime_false_when_nothing_usable_exists(tmp_path):
    repo = _git_repo(tmp_path)
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_snapshot(registry, _snapshot(repo))
    router = ProjectRuntimeRouter(registry, _bootstrap(None))
    shutil.rmtree(repo)

    assert router.has_any_routable_runtime() is False
