"""Tests for core.sandbox_workspace (Step 14b-3: git worktree management).

Tests use a fake _SubprocessRunner so we don't need real git or a real
sandbox repo on the CI runner. Integration tests with real git are gated
with a fixture that creates a tmp git repo on demand.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from core.sandbox_workspace import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_BRANCH_PREFIX,
    SandboxConfig,
    SandboxError,
    SandboxWorkspace,
    WorktreeHandle,
    _DefaultSubprocessRunner,
    _RunResult,
    _SubprocessRunner,
    to_dict_handles,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunner(_SubprocessRunner):
    """Records every git invocation; returns canned RunResults in order.

    If `responses` is exhausted, returns success (returncode=0) by default.
    """

    def __init__(self, responses: list[_RunResult] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def run(self, cmd, cwd, env, timeout):
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        if self.responses:
            return self.responses.pop(0)
        return _RunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Creates a minimal directory that looks like a git repo (has .git/)."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    """Initialises a real git repo with one commit. Skipped if git missing."""
    if shutil.which("git") is None:
        pytest.skip("git binary not available")
    repo = tmp_path / "real_main"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# test\n")
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    return repo


def _make_config(repo: Path, tmp_path: Path) -> SandboxConfig:
    return SandboxConfig(
        main_repo_path=repo,
        worktree_root=tmp_path / "worktrees",
    )


# ---------------------------------------------------------------------------
# SandboxConfig validation
# ---------------------------------------------------------------------------


def test_config_happy_path(fake_repo, tmp_path):
    cfg = SandboxConfig(
        main_repo_path=fake_repo,
        worktree_root=tmp_path / "wt",
    )
    assert cfg.branch_prefix == DEFAULT_BRANCH_PREFIX
    assert cfg.base_branch == DEFAULT_BASE_BRANCH


def test_config_is_frozen(fake_repo, tmp_path):
    cfg = SandboxConfig(main_repo_path=fake_repo, worktree_root=tmp_path / "wt")
    with pytest.raises(Exception):
        cfg.base_branch = "other"  # type: ignore[misc]


def test_config_rejects_non_path_main_repo(tmp_path):
    with pytest.raises(ValueError, match="main_repo_path_must_be_path"):
        SandboxConfig(
            main_repo_path="not_a_path",  # type: ignore[arg-type]
            worktree_root=tmp_path / "wt",
        )


def test_config_rejects_missing_main_repo(tmp_path):
    with pytest.raises(ValueError, match="main_repo_missing"):
        SandboxConfig(
            main_repo_path=tmp_path / "doesnt_exist",
            worktree_root=tmp_path / "wt",
        )


def test_config_rejects_main_without_dot_git(tmp_path):
    bad_repo = tmp_path / "no_git"
    bad_repo.mkdir()
    with pytest.raises(ValueError, match="main_repo_not_git"):
        SandboxConfig(
            main_repo_path=bad_repo,
            worktree_root=tmp_path / "wt",
        )


def test_config_rejects_worktree_root_inside_main_repo(fake_repo):
    with pytest.raises(ValueError, match="worktree_root_inside_main_repo"):
        SandboxConfig(
            main_repo_path=fake_repo,
            worktree_root=fake_repo / "subdir",
        )


def test_config_rejects_empty_branch_prefix(fake_repo, tmp_path):
    with pytest.raises(ValueError, match="empty_branch_prefix"):
        SandboxConfig(
            main_repo_path=fake_repo,
            worktree_root=tmp_path / "wt",
            branch_prefix="",
        )


def test_config_rejects_branch_prefix_with_shell_meta(fake_repo, tmp_path):
    with pytest.raises(ValueError, match="shell_meta_in_branch_prefix"):
        SandboxConfig(
            main_repo_path=fake_repo,
            worktree_root=tmp_path / "wt",
            branch_prefix="bad;name",
        )


def test_config_rejects_invalid_base_branch(fake_repo, tmp_path):
    with pytest.raises(ValueError, match="invalid_base_branch"):
        SandboxConfig(
            main_repo_path=fake_repo,
            worktree_root=tmp_path / "wt",
            base_branch="bad name with spaces",
        )


@pytest.mark.parametrize("bad", [0, -1, True])
def test_config_rejects_invalid_timeout(fake_repo, tmp_path, bad):
    with pytest.raises(ValueError, match="invalid_git_timeout"):
        SandboxConfig(
            main_repo_path=fake_repo,
            worktree_root=tmp_path / "wt",
            git_timeout_seconds=bad,
        )


# ---------------------------------------------------------------------------
# WorktreeHandle validation
# ---------------------------------------------------------------------------


def test_handle_happy_path():
    h = WorktreeHandle(
        task_id="task-42",
        branch="feature/task-42",
        path=Path("/tmp/x"),
        created_at=1.0,
    )
    assert h.task_id == "task-42"


def test_handle_is_frozen():
    h = WorktreeHandle(
        task_id="t1", branch="feature/t1", path=Path("/tmp"), created_at=1.0,
    )
    with pytest.raises(Exception):
        h.task_id = "t2"  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "Has-Capital", "with space", "../traverse",
     "task;rm", "x" * 65, "_starts_with_underscore"],
)
def test_handle_rejects_invalid_task_id(bad):
    with pytest.raises(ValueError, match="invalid_task_id"):
        WorktreeHandle(
            task_id=bad,
            branch="feature/x",
            path=Path("/tmp"),
            created_at=1.0,
        )


def test_handle_accepts_dashes_underscores_digits():
    for good in ["task-42", "abc_def", "1234", "a-b_c-1"]:
        WorktreeHandle(
            task_id=good,
            branch=f"feature/{good}",
            path=Path("/tmp"),
            created_at=1.0,
        )


# ---------------------------------------------------------------------------
# SandboxWorkspace.acquire — argv assembly
# ---------------------------------------------------------------------------


def test_acquire_runs_correct_git_worktree_add(fake_repo, tmp_path):
    runner = FakeRunner()
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=runner)
    handle = ws.acquire("task-42")
    assert handle.task_id == "task-42"
    assert handle.branch == "feature/task-42"
    # First call should be `git worktree add -b feature/task-42 <path> main`
    cmd = runner.calls[0]["cmd"]
    assert cmd[:4] == ("git", "worktree", "add", "-b")
    assert cmd[4] == "feature/task-42"
    assert cmd[5].endswith("/task-42")
    assert cmd[6] == "main"


def test_acquire_creates_worktree_root_directory(fake_repo, tmp_path):
    root = tmp_path / "deep" / "nested" / "wt"
    cfg = SandboxConfig(main_repo_path=fake_repo, worktree_root=root)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    ws.acquire("t1")
    assert root.exists()


def test_acquire_rejects_invalid_task_id(fake_repo, tmp_path):
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_task_id"):
        ws.acquire("Bad/Name")


def test_acquire_rejects_path_traversal_task_id(fake_repo, tmp_path):
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_task_id"):
        ws.acquire("../etc/passwd")


def test_acquire_raises_when_worktree_path_exists(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    cfg.worktree_root.mkdir(parents=True)
    (cfg.worktree_root / "task-42").mkdir()
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(SandboxError) as exc_info:
        ws.acquire("task-42")
    assert exc_info.value.code == "worktree_exists"


def test_acquire_raises_on_git_failure(fake_repo, tmp_path):
    runner = FakeRunner(
        responses=[_RunResult(returncode=128, stdout="", stderr="fatal: nope")],
    )
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=runner)
    with pytest.raises(SandboxError) as exc_info:
        ws.acquire("task-42")
    assert exc_info.value.code == "git_failed"
    assert "fatal: nope" in exc_info.value.detail


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------


def test_release_runs_git_worktree_remove(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    cfg.worktree_root.mkdir(parents=True)
    wt_path = cfg.worktree_root / "task-42"
    wt_path.mkdir()
    handle = WorktreeHandle(
        task_id="task-42",
        branch="feature/task-42",
        path=wt_path,
        created_at=1.0,
    )
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.release(handle)
    cmds = [c["cmd"] for c in runner.calls]
    # Should call: worktree remove --force, worktree prune
    assert any("remove" in c for c in cmds)
    assert any("prune" in c for c in cmds)


def test_release_idempotent_on_missing_path(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = WorktreeHandle(
        task_id="task-42",
        branch="feature/task-42",
        path=cfg.worktree_root / "ghost",
        created_at=1.0,
    )
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.release(handle)  # should not raise
    # Should still call prune at least
    cmds = [c["cmd"] for c in runner.calls]
    assert any("prune" in c for c in cmds)


def test_release_with_delete_branch_calls_branch_d(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    cfg.worktree_root.mkdir(parents=True)
    wt_path = cfg.worktree_root / "task-42"
    wt_path.mkdir()
    handle = WorktreeHandle(
        task_id="task-42",
        branch="feature/task-42",
        path=wt_path,
        created_at=1.0,
    )
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.release(handle, delete_branch=True)
    cmds = [c["cmd"] for c in runner.calls]
    assert any(c[:3] == ("git", "branch", "-D") for c in cmds)


def test_release_falls_back_to_rmtree_if_git_fails(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    cfg.worktree_root.mkdir(parents=True)
    wt_path = cfg.worktree_root / "task-42"
    wt_path.mkdir()
    (wt_path / "marker.txt").write_text("x")
    handle = WorktreeHandle(
        task_id="task-42",
        branch="feature/task-42",
        path=wt_path,
        created_at=1.0,
    )
    runner = FakeRunner(
        responses=[_RunResult(returncode=128, stdout="", stderr="not a worktree")],
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.release(handle)
    assert not wt_path.exists()  # fallback rmtree must have removed it


def test_release_rejects_non_handle(fake_repo, tmp_path):
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_handle_type"):
        ws.release("not a handle")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# commit_in_worktree
# ---------------------------------------------------------------------------


def _handle_for(cfg: SandboxConfig, task_id: str = "task-42") -> WorktreeHandle:
    wt_path = cfg.worktree_root / task_id
    wt_path.mkdir(parents=True, exist_ok=True)
    return WorktreeHandle(
        task_id=task_id,
        branch=f"feature/{task_id}",
        path=wt_path,
        created_at=1.0,
    )


def test_commit_runs_add_status_commit_revparse(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    runner = FakeRunner(
        responses=[
            _RunResult(returncode=0, stdout="", stderr=""),                  # add
            _RunResult(returncode=0, stdout=" M file.py\n", stderr=""),      # status
            _RunResult(returncode=0, stdout="", stderr=""),                  # commit
            _RunResult(returncode=0, stdout="abc123def\n", stderr=""),       # rev-parse
        ],
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    sha = ws.commit_in_worktree(
        handle,
        message="add new function",
        author_name="Bot",
        author_email="bot@example.com",
    )
    assert sha == "abc123def"


def test_commit_raises_on_nothing_to_commit(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    runner = FakeRunner(
        responses=[
            _RunResult(returncode=0, stdout="", stderr=""),  # add
            _RunResult(returncode=0, stdout="", stderr=""),  # status — empty
        ],
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    with pytest.raises(SandboxError) as exc_info:
        ws.commit_in_worktree(
            handle,
            message="empty",
            author_name="Bot",
            author_email="bot@example.com",
        )
    assert exc_info.value.code == "nothing_to_commit"


def test_commit_rejects_empty_message(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="empty_commit_message"):
        ws.commit_in_worktree(
            handle, message="  ",
            author_name="Bot", author_email="bot@example.com",
        )


def test_commit_rejects_invalid_email(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_author_email"):
        ws.commit_in_worktree(
            handle, message="m",
            author_name="Bot", author_email="not-an-email",
        )


def test_commit_raises_on_missing_worktree(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = WorktreeHandle(
        task_id="task-42",
        branch="feature/task-42",
        path=cfg.worktree_root / "ghost",
        created_at=1.0,
    )
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(SandboxError) as exc_info:
        ws.commit_in_worktree(
            handle, message="m",
            author_name="Bot", author_email="bot@example.com",
        )
    assert exc_info.value.code == "worktree_missing"


# ---------------------------------------------------------------------------
# push_branch
# ---------------------------------------------------------------------------


def test_push_branch_runs_git_push(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.push_branch(handle)
    cmd = runner.calls[0]["cmd"]
    assert cmd == ("git", "push", "origin", "feature/task-42")


def test_push_branch_uses_custom_remote(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.push_branch(handle, remote="upstream")
    cmd = runner.calls[0]["cmd"]
    assert cmd[2] == "upstream"


def test_push_branch_rejects_invalid_remote(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_remote_name"):
        ws.push_branch(handle, remote="bad;name")


def test_push_branch_raises_on_failure(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    handle = _handle_for(cfg)
    runner = FakeRunner(
        responses=[_RunResult(returncode=1, stdout="", stderr="fatal: rejected")],
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    with pytest.raises(SandboxError) as exc_info:
        ws.push_branch(handle)
    assert exc_info.value.code == "git_push_failed"


# ---------------------------------------------------------------------------
# push_named_branch (Step 16) — canonical post-release push
# ---------------------------------------------------------------------------


def test_push_named_branch_runs_git_push_from_main_repo(fake_repo, tmp_path):
    """Happy path: correct git command, runs from main_repo_path."""
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.push_named_branch("feature/task-99")
    cmd = runner.calls[0]["cmd"]
    cwd = runner.calls[0]["cwd"]
    assert cmd == ("git", "push", "origin", "feature/task-99")
    assert cwd == str(cfg.main_repo_path)


def test_push_named_branch_custom_remote(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.push_named_branch("feature/task-99", remote="upstream")
    assert runner.calls[0]["cmd"][2] == "upstream"


def test_push_named_branch_rejects_invalid_branch(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_branch"):
        ws.push_named_branch("bad branch name!")


def test_push_named_branch_rejects_invalid_remote(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_remote_name"):
        ws.push_named_branch("feature/ok", remote="bad;remote")


def test_push_named_branch_raises_sandbox_error_on_git_failure(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner(
        responses=[_RunResult(returncode=1, stdout="", stderr="fatal: rejected")]
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    with pytest.raises(SandboxError) as exc_info:
        ws.push_named_branch("feature/task-99")
    assert exc_info.value.code == "git_push_failed"


def test_push_named_branch_works_without_worktree_on_disk(fake_repo, tmp_path):
    """Must NOT require the worktree path to exist — runs from main_repo."""
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    worktree_path = cfg.worktree_root / "task-99"
    assert not worktree_path.exists()
    ws.push_named_branch("feature/task-99")
    assert len(runner.calls) == 1


# push_branch_from_main — backward-compat alias for push_named_branch
# ---------------------------------------------------------------------------


def test_push_branch_from_main_runs_git_push_from_main_repo(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.push_branch_from_main("feature/task-99")
    cmd = runner.calls[0]["cmd"]
    cwd = runner.calls[0]["cwd"]
    assert cmd == ("git", "push", "origin", "feature/task-99")
    assert cwd == str(cfg.main_repo_path)


def test_push_branch_from_main_uses_custom_remote(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    ws.push_branch_from_main("feature/task-99", remote="upstream")
    cmd = runner.calls[0]["cmd"]
    assert cmd[2] == "upstream"


def test_push_branch_from_main_rejects_invalid_branch(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_branch"):
        ws.push_branch_from_main("bad branch name!")


def test_push_branch_from_main_rejects_invalid_remote(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    ws = SandboxWorkspace(cfg, runner=FakeRunner())
    with pytest.raises(ValueError, match="invalid_remote_name"):
        ws.push_branch_from_main("feature/ok", remote="bad;remote")


def test_push_branch_from_main_raises_sandbox_error_on_git_failure(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner(
        responses=[_RunResult(returncode=1, stdout="", stderr="fatal: rejected")]
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    with pytest.raises(SandboxError) as exc_info:
        ws.push_branch_from_main("feature/task-99")
    assert exc_info.value.code == "git_push_failed"


def test_push_branch_from_main_works_without_worktree_on_disk(fake_repo, tmp_path):
    """Specifically: the method must NOT require the worktree path to exist."""
    cfg = _make_config(fake_repo, tmp_path)
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    worktree_path = cfg.worktree_root / "task-99"
    assert not worktree_path.exists()
    ws.push_branch_from_main("feature/task-99")
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# list_worktrees / cleanup_orphans
# ---------------------------------------------------------------------------


def test_list_worktrees_parses_porcelain_output(fake_repo, tmp_path):
    runner = FakeRunner(
        responses=[_RunResult(
            returncode=0,
            stdout=(
                "worktree /tmp/main\n"
                "HEAD abc\n"
                "branch refs/heads/main\n"
                "\n"
                "worktree /tmp/aidt_worktrees/task-42\n"
                "HEAD def\n"
                "branch refs/heads/feature/task-42\n"
            ),
            stderr="",
        )],
    )
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=runner)
    paths = ws.list_worktrees()
    assert "/tmp/main" in paths
    assert "/tmp/aidt_worktrees/task-42" in paths


def test_cleanup_orphans_returns_zero_when_root_missing(fake_repo, tmp_path):
    cfg = SandboxConfig(
        main_repo_path=fake_repo,
        worktree_root=tmp_path / "missing",
    )
    runner = FakeRunner()
    ws = SandboxWorkspace(cfg, runner=runner)
    assert ws.cleanup_orphans() == 0


def test_cleanup_orphans_removes_untracked_directories(fake_repo, tmp_path):
    cfg = _make_config(fake_repo, tmp_path)
    cfg.worktree_root.mkdir(parents=True)
    tracked = cfg.worktree_root / "tracked"
    tracked.mkdir()
    orphan = cfg.worktree_root / "orphan"
    orphan.mkdir()
    (orphan / "file.txt").write_text("garbage")

    runner = FakeRunner(
        responses=[
            _RunResult(returncode=0, stdout=f"worktree {tracked.resolve()}\n", stderr=""),
            _RunResult(returncode=0, stdout="", stderr=""),  # prune
        ],
    )
    ws = SandboxWorkspace(cfg, runner=runner)
    removed = ws.cleanup_orphans()
    assert removed == 1
    assert not orphan.exists()
    assert tracked.exists()


# ---------------------------------------------------------------------------
# Subprocess sanitisation — defence in depth
# ---------------------------------------------------------------------------


def test_run_git_rejects_shell_meta_in_args(fake_repo, tmp_path):
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=FakeRunner())
    with pytest.raises(ValueError, match="shell_meta_in_git_arg"):
        ws._run_git(fake_repo, ("status", "&&", "rm", "-rf", "/"))


def test_run_git_rejects_non_string_arg(fake_repo, tmp_path):
    ws = SandboxWorkspace(_make_config(fake_repo, tmp_path), runner=FakeRunner())
    with pytest.raises(ValueError, match="non_string_git_arg"):
        ws._run_git(fake_repo, ("status", 42))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DefaultSubprocessRunner — exception handling
# ---------------------------------------------------------------------------


def test_default_runner_handles_timeout(monkeypatch):
    runner = _DefaultSubprocessRunner()

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.run(
        cmd=("git", "status"), cwd="/tmp", env={}, timeout=1,
    )
    assert result.returncode == 124
    assert "timeout" in result.stderr


def test_default_runner_handles_file_not_found(monkeypatch):
    runner = _DefaultSubprocessRunner()

    def fake_run(*a, **kw):
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.run(
        cmd=("git", "status"), cwd="/tmp", env={}, timeout=1,
    )
    assert result.returncode == 127
    assert "git_not_found" in result.stderr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_to_dict_handles():
    h1 = WorktreeHandle(
        task_id="t1", branch="feature/t1", path=Path("/tmp/t1"), created_at=1.0,
    )
    h2 = WorktreeHandle(
        task_id="t2", branch="feature/t2", path=Path("/tmp/t2"), created_at=2.0,
    )
    out = to_dict_handles([h1, h2])
    assert len(out) == 2
    assert out[0]["task_id"] == "t1"
    assert out[0]["branch"] == "feature/t1"
    assert out[1]["created_at"] == 2.0


# ---------------------------------------------------------------------------
# INTEGRATION TESTS — real git, gated by `git` binary availability
# ---------------------------------------------------------------------------


def test_integration_acquire_release_with_real_git(real_repo, tmp_path):
    cfg = SandboxConfig(
        main_repo_path=real_repo,
        worktree_root=tmp_path / "real_wt",
    )
    ws = SandboxWorkspace(cfg)
    handle = ws.acquire("realtask")
    try:
        assert handle.path.exists()
        assert (handle.path / "README.md").exists()  # base content present
    finally:
        ws.release(handle)
    assert not handle.path.exists()


def test_integration_commit_in_real_worktree(real_repo, tmp_path):
    cfg = SandboxConfig(
        main_repo_path=real_repo,
        worktree_root=tmp_path / "real_wt",
    )
    ws = SandboxWorkspace(cfg)
    handle = ws.acquire("commit-test")
    try:
        (handle.path / "new_file.txt").write_text("new content")
        sha = ws.commit_in_worktree(
            handle,
            message="test commit",
            author_name="AI Dev Team Bot",
            author_email="bot@ai-dev-team.local",
        )
        assert len(sha) == 40  # full SHA1
    finally:
        ws.release(handle)


def test_integration_cleanup_orphans_with_real_git(real_repo, tmp_path):
    cfg = SandboxConfig(
        main_repo_path=real_repo,
        worktree_root=tmp_path / "real_wt",
    )
    ws = SandboxWorkspace(cfg)
    # Make an orphan (a directory the bot didn't create via acquire)
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    orphan = cfg.worktree_root / "orphan_dir"
    orphan.mkdir()
    (orphan / "junk.txt").write_text("garbage")
    removed = ws.cleanup_orphans()
    assert removed == 1
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# gh_pr_create (Step 17)
# ---------------------------------------------------------------------------


def _ws_with_canned_gh(fake_repo, tmp_path, *, gh_returncode=0,
                        gh_stdout="", gh_stderr=""):
    """Build a SandboxWorkspace whose runner returns canned `gh` results."""

    class _GhCanned(_SubprocessRunner):
        def __init__(self):
            self.calls = []

        def run(self, cmd, cwd, env, timeout):
            self.calls.append({"cmd": cmd, "cwd": cwd})
            if cmd[0] == "gh":
                return _RunResult(
                    returncode=gh_returncode,
                    stdout=gh_stdout,
                    stderr=gh_stderr,
                )
            return _RunResult(returncode=0, stdout="", stderr="")

    cfg = SandboxConfig(main_repo_path=fake_repo, worktree_root=tmp_path / "wt")
    runner = _GhCanned()
    return SandboxWorkspace(cfg, runner=runner), runner


def test_gh_pr_create_happy_path(fake_repo, tmp_path):
    pr_url = "https://github.com/user/repo/pull/42"
    ws, runner = _ws_with_canned_gh(
        fake_repo, tmp_path,
        gh_stdout=f"Creating pull request\n{pr_url}\n",
    )
    url = ws.gh_pr_create(
        "feature/task-001",
        title="My PR",
        body="Some body text.",
    )
    assert url == pr_url
    gh_call = next(c for c in runner.calls if c["cmd"][0] == "gh")
    assert gh_call["cmd"][:3] == ("gh", "pr", "create")
    assert "--draft" in gh_call["cmd"]
    assert "--head" in gh_call["cmd"]
    assert "--base" in gh_call["cmd"]


def test_gh_pr_create_invalid_branch(fake_repo, tmp_path):
    ws, _ = _ws_with_canned_gh(fake_repo, tmp_path)
    with pytest.raises(ValueError, match="invalid_branch"):
        ws.gh_pr_create("BAD;BRANCH", title="t", body="b")


def test_gh_pr_create_invalid_base(fake_repo, tmp_path):
    ws, _ = _ws_with_canned_gh(fake_repo, tmp_path)
    with pytest.raises(ValueError, match="invalid_base"):
        ws.gh_pr_create("feature/x", title="t", body="b", base="bad;base")


@pytest.mark.parametrize("bad", ["", "  "])
def test_gh_pr_create_empty_title(fake_repo, tmp_path, bad):
    ws, _ = _ws_with_canned_gh(fake_repo, tmp_path)
    with pytest.raises(ValueError, match="empty_title"):
        ws.gh_pr_create("feature/x", title=bad, body="b")


def test_gh_pr_create_long_title(fake_repo, tmp_path):
    ws, _ = _ws_with_canned_gh(fake_repo, tmp_path)
    with pytest.raises(ValueError, match="title_too_long"):
        ws.gh_pr_create("feature/x", title="X" * 257, body="b")


def test_gh_pr_create_null_in_body(fake_repo, tmp_path):
    ws, _ = _ws_with_canned_gh(fake_repo, tmp_path)
    with pytest.raises(ValueError, match="invalid_body_chars"):
        ws.gh_pr_create("feature/x", title="t", body="bad\x00body")


def test_gh_pr_create_shell_meta_in_branch(fake_repo, tmp_path):
    """shell-meta IS rejected in branch (used as git ref) — but allowed in title/body
    (freeform PR content; backticks for markdown code formatting are common)."""
    ws, _ = _ws_with_canned_gh(fake_repo, tmp_path)
    with pytest.raises(ValueError, match="invalid_branch"):
        ws.gh_pr_create("feature/x;rm-rf", title="t", body="b")


def test_gh_pr_create_allows_backticks_in_body(fake_repo, tmp_path):
    """Markdown code-formatting backticks in PR body must NOT raise. They go
    through subprocess argv, never through a shell."""
    ws, _ = _ws_with_canned_gh(
        fake_repo, tmp_path,
        gh_stdout="https://github.com/x/y/pull/1\n",
    )
    url = ws.gh_pr_create(
        "feature/x",
        title="My PR with `code`",
        body="Body with `task-id` and `branch` references.",
    )
    assert url == "https://github.com/x/y/pull/1"


def test_gh_pr_create_gh_not_found(fake_repo, tmp_path):
    ws, _ = _ws_with_canned_gh(
        fake_repo, tmp_path,
        gh_returncode=127,
        gh_stderr="gh: command not found",
    )
    with pytest.raises(SandboxError) as exc_info:
        ws.gh_pr_create("feature/x", title="t", body="b")
    assert exc_info.value.code == "gh_not_found"


def test_gh_pr_create_failure(fake_repo, tmp_path):
    ws, _ = _ws_with_canned_gh(
        fake_repo, tmp_path,
        gh_returncode=1,
        gh_stderr="GraphQL error: Resource not accessible",
    )
    with pytest.raises(SandboxError) as exc_info:
        ws.gh_pr_create("feature/x", title="t", body="b")
    assert exc_info.value.code == "gh_pr_create_failed"


def test_gh_pr_create_returns_stdout_when_no_url(fake_repo, tmp_path):
    """If `gh` succeeds but stdout doesn't include a URL — return stdout as-is."""
    ws, _ = _ws_with_canned_gh(
        fake_repo, tmp_path,
        gh_stdout="Created PR (no URL parseable)\n",
    )
    result = ws.gh_pr_create("feature/x", title="t", body="b")
    assert "Created PR" in result
