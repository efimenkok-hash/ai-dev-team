"""Tests for core.project_runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.adapter import ProjectAdapter, ProjectCommand, ProjectRule
from core.project_runtime import ProjectRuntimeBinding
from core.sandbox_workspace import SandboxConfig


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _rule(**overrides: object) -> ProjectRule:
    data: dict[str, object] = {
        "name": "ascii_only",
        "description": "forbid non-ASCII strings",
        "severity": "error",
    }
    data.update(overrides)
    return ProjectRule(**data)


def _command(**overrides: object) -> ProjectCommand:
    data: dict[str, object] = {
        "name": "test",
        "cmd": ("pytest", "-q"),
        "timeout_seconds": 120,
    }
    data.update(overrides)
    return ProjectCommand(**data)


def _binding(repo_path: Path, **overrides: object) -> ProjectRuntimeBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / "worktrees",
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (_rule(),),
        "commands": (
            _command(name="lint", cmd=("ruff", "check", ".")),
            _command(name="test", cmd=("pytest", "-q")),
        ),
        "forbidden_paths": ("secrets/",),
        "forbidden_tokens": ("API_KEY",),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_project_runtime_binding_happy_path(tmp_path: Path):
    repo = _git_repo(tmp_path)
    binding = _binding(repo)

    assert binding.project_id == "alpha_project"
    assert binding.adapter_name == "alpha_adapter"
    assert binding.repo_path == repo.resolve()
    assert binding.worktree_root == (tmp_path / "worktrees").resolve()
    assert binding.base_branch == "main"
    assert binding.branch_prefix == "feature/"
    assert binding.language == "python"


def test_project_runtime_binding_is_frozen(tmp_path: Path):
    binding = _binding(_git_repo(tmp_path))
    with pytest.raises(Exception):
        binding.base_branch = "develop"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "  ", None, "bad-id", "Русский"])
def test_project_runtime_binding_rejects_bad_project_id(tmp_path: Path, bad: object):
    with pytest.raises(ValueError):
        _binding(_git_repo(tmp_path), project_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", "Bad-Name", "has space", "русский"])
def test_project_runtime_binding_rejects_bad_adapter_name(tmp_path: Path, bad: str):
    with pytest.raises(ValueError):
        _binding(_git_repo(tmp_path), adapter_name=bad)


def test_project_runtime_binding_rejects_bad_repo_path_type(tmp_path: Path):
    with pytest.raises(ValueError, match="repo_path_must_be_path"):
        ProjectRuntimeBinding(
            project_id="alpha_project",
            adapter_name="alpha_adapter",
            repo_path="bad",  # type: ignore[arg-type]
        )


def test_project_runtime_binding_rejects_missing_repo_path(tmp_path: Path):
    with pytest.raises(ValueError, match="repo_path_missing"):
        _binding(tmp_path / "missing")


def test_project_runtime_binding_rejects_non_dir_repo_path(tmp_path: Path):
    repo_file = tmp_path / "repo.txt"
    repo_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="repo_path_not_dir"):
        _binding(repo_file)


def test_project_runtime_binding_rejects_non_git_repo_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="repo_path_not_git"):
        _binding(repo)


def test_project_runtime_binding_rejects_invalid_worktree_root_type(tmp_path: Path):
    with pytest.raises(ValueError, match="worktree_root_must_be_path_or_none"):
        _binding(_git_repo(tmp_path), worktree_root="bad")  # type: ignore[arg-type]


def test_project_runtime_binding_rejects_worktree_root_file(tmp_path: Path):
    repo = _git_repo(tmp_path)
    worktree_file = tmp_path / "wt-file"
    worktree_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="worktree_root_not_dir"):
        _binding(repo, worktree_root=worktree_file)


def test_project_runtime_binding_rejects_worktree_root_inside_repo(tmp_path: Path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="worktree_root_inside_repo_path"):
        _binding(repo, worktree_root=repo / "worktrees")


@pytest.mark.parametrize("bad", ["", "  ", "bad name", "bad;name"])
def test_project_runtime_binding_rejects_invalid_base_branch(tmp_path: Path, bad: str):
    with pytest.raises(ValueError):
        _binding(_git_repo(tmp_path), base_branch=bad)


@pytest.mark.parametrize("bad", ["", "  ", "bad;prefix", "bad prefix"])
def test_project_runtime_binding_rejects_invalid_branch_prefix(tmp_path: Path, bad: str):
    with pytest.raises(ValueError):
        _binding(_git_repo(tmp_path), branch_prefix=bad)


def test_project_runtime_binding_rejects_invalid_language(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown_language"):
        _binding(_git_repo(tmp_path), language="cobol")


def test_project_runtime_binding_rejects_non_tuple_rules(tmp_path: Path):
    with pytest.raises(ValueError, match="rules_must_be_tuple"):
        _binding(_git_repo(tmp_path), rules=[_rule()])  # type: ignore[arg-type]


def test_project_runtime_binding_rejects_invalid_rule_item(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid_rule_type"):
        _binding(_git_repo(tmp_path), rules=(_rule(), "bad"))  # type: ignore[arg-type]


def test_project_runtime_binding_rejects_non_tuple_commands(tmp_path: Path):
    with pytest.raises(ValueError, match="commands_must_be_tuple"):
        _binding(_git_repo(tmp_path), commands=[_command()])  # type: ignore[arg-type]


def test_project_runtime_binding_rejects_invalid_command_item(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid_command_type"):
        _binding(
            _git_repo(tmp_path),
            commands=(_command(), "bad"),  # type: ignore[arg-type]
        )


def test_project_runtime_binding_rejects_duplicate_command_names(tmp_path: Path):
    with pytest.raises(ValueError, match="duplicate_command_name:test"):
        _binding(
            _git_repo(tmp_path),
            commands=(
                _command(name="test", cmd=("pytest", "-q")),
                _command(name="test", cmd=("pytest", "-x")),
            ),
        )


def test_project_runtime_binding_rejects_bad_forbidden_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="forbidden_paths_must_be_tuple"):
        _binding(_git_repo(tmp_path), forbidden_paths=["secrets/"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty_forbidden_path"):
        _binding(_git_repo(tmp_path), forbidden_paths=("  ",))


def test_project_runtime_binding_rejects_bad_forbidden_tokens(tmp_path: Path):
    with pytest.raises(ValueError, match="forbidden_tokens_must_be_tuple"):
        _binding(_git_repo(tmp_path), forbidden_tokens=["API_KEY"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty_forbidden_token"):
        _binding(_git_repo(tmp_path), forbidden_tokens=(" ",))


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def test_build_sandbox_config_round_trips_fields(tmp_path: Path):
    repo = _git_repo(tmp_path)
    binding = _binding(
        repo,
        worktree_root=tmp_path / "custom-worktrees",
        base_branch="develop",
        branch_prefix="task/",
    )

    cfg = binding.build_sandbox_config()

    assert isinstance(cfg, SandboxConfig)
    assert cfg.main_repo_path == repo.resolve()
    assert cfg.worktree_root == (tmp_path / "custom-worktrees").resolve()
    assert cfg.base_branch == "develop"
    assert cfg.branch_prefix == "task/"


def test_build_adapter_returns_real_project_adapter(tmp_path: Path):
    repo = _git_repo(tmp_path)
    binding = _binding(repo)

    adapter = binding.build_adapter(repo)

    assert isinstance(adapter, ProjectAdapter)
    assert adapter.name == "alpha_adapter"
    assert adapter.project_path == repo.resolve()
    assert adapter.language == "python"


def test_build_adapter_inherits_rules_commands_and_forbidden_lists(tmp_path: Path):
    repo = _git_repo(tmp_path)
    rule = _rule(name="no_unicode", description="forbid unicode")
    lint = _command(name="lint", cmd=("ruff", "check", "."))
    test_cmd = _command(name="test", cmd=("pytest", "-q"))
    binding = _binding(
        repo,
        rules=(rule,),
        commands=(lint, test_cmd),
        forbidden_paths=("secrets/", "node_modules/"),
        forbidden_tokens=("API_KEY", "SECRET"),
    )

    adapter = binding.build_adapter(repo)

    assert adapter.rules == (rule,)
    assert adapter.get_command("lint") == lint
    assert adapter.get_command("test") == test_cmd
    assert adapter.forbidden_paths == ("secrets/", "node_modules/")
    assert adapter.forbidden_tokens == ("API_KEY", "SECRET")


def test_build_adapter_works_for_different_existing_path_than_repo_path(tmp_path: Path):
    repo = _git_repo(tmp_path, name="main-repo")
    worktree = tmp_path / "worktree-copy"
    worktree.mkdir()
    (worktree / "README.md").write_text("worktree\n", encoding="utf-8")
    binding = _binding(repo)

    adapter = binding.build_adapter(worktree)

    assert adapter.project_path == worktree.resolve()
    assert adapter.name == binding.adapter_name


def test_build_adapter_rejects_non_path(tmp_path: Path):
    binding = _binding(_git_repo(tmp_path))
    with pytest.raises(ValueError, match="project_path_must_be_path"):
        binding.build_adapter("bad")  # type: ignore[arg-type]
