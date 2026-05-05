"""
core/sandbox_workspace.py

Step 14b-3: git worktree management for isolated task execution.

Each pipeline task gets its own worktree under /tmp/aidt_worktrees/<task_id>.
The agent team writes code there, runtime_validator runs ruff/pytest there,
and on completion the bot can `git push origin <branch>` to GitHub. Then
the worktree is removed and disk space returns to baseline (~5-15 MB for
the main repo).

This is the spec's "worktree" approach (chosen by the user): keeps total
disk usage bounded, doesn't lose work on Mac restart (branches persist
in the main repo or in GitHub), and gives each task a clean filesystem
that can't accidentally affect other in-flight work.

CONTRACTS:
1. SandboxConfig is frozen; all paths resolved at construction; main_repo
   must exist and be a git repo.
2. WorktreeHandle is frozen; created only by SandboxWorkspace.acquire().
3. acquire() is the only way to create a worktree. It validates task_id
   against a strict regex (snake_case-with-digits) and rejects shell-meta
   chars. Branch name = `<prefix><task_id>`.
4. release() removes the worktree directory + the git worktree record.
   Idempotent — calling on an already-released handle is a no-op.
5. All git commands run via subprocess.run with stdin=DEVNULL, no shell,
   captured output, deterministic env, timeout. Token-by-token argv.
6. push_branch() requires that the main_repo has a remote named `origin`
   (or whichever the caller specifies). Fails fast on missing remote.
7. cleanup_orphans() looks for /tmp/aidt_worktrees/* directories that
   are no longer registered in `git worktree list` and removes them.
   Used by the bot's `/cleanup` command and on startup.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRANCH_PREFIX = "feature/"
DEFAULT_BASE_BRANCH = "main"
DEFAULT_WORKTREE_ROOT = Path(tempfile.gettempdir()) / "aidt_worktrees"
DEFAULT_GIT_TIMEOUT_SECONDS = 60

# Strict task_id rule: lowercase ASCII letters/digits/-/_, 1-64 chars,
# starts with a letter or digit. Rejects path traversal, shell metas,
# unicode tricks. Branch names derived from it inherit the safety.
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REMOTE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")

# Forbidden tokens in any git command argv (defence in depth).
_SHELL_META_TOKENS = (";", "|", "&", ">", "<", "`", "$(", "\n", "\r")

# Env vars carried over to git subprocesses (deterministic, no leakage).
_PASSTHROUGH_ENV = ("PATH", "HOME", "USER", "SSH_AUTH_SOCK", "SSH_AGENT_PID")
_BASE_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_TERMINAL_PROMPT": "0",  # never interactively prompt
}


class SandboxError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SandboxConfig:
    main_repo_path: Path
    worktree_root: Path = DEFAULT_WORKTREE_ROOT
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    base_branch: str = DEFAULT_BASE_BRANCH
    git_timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.main_repo_path, Path):
            raise ValueError("main_repo_path_must_be_path")
        resolved_main = self.main_repo_path.resolve()
        if not resolved_main.exists():
            raise ValueError(f"main_repo_missing:{resolved_main}")
        if not resolved_main.is_dir():
            raise ValueError(f"main_repo_not_dir:{resolved_main}")
        if not (resolved_main / ".git").exists():
            raise ValueError(f"main_repo_not_git:{resolved_main}")

        if not isinstance(self.worktree_root, Path):
            raise ValueError("worktree_root_must_be_path")
        resolved_root = self.worktree_root.resolve()
        # Worktree root may not exist yet — we'll create on demand.
        # But it must NOT live inside the main repo (would confuse git).
        try:
            resolved_root.relative_to(resolved_main)
            raise ValueError("worktree_root_inside_main_repo")
        except ValueError as exc:
            if "worktree_root_inside_main_repo" in str(exc):
                raise

        if not isinstance(self.branch_prefix, str) or not self.branch_prefix.strip():
            raise ValueError("empty_branch_prefix")
        if any(meta in self.branch_prefix for meta in _SHELL_META_TOKENS):
            raise ValueError(f"shell_meta_in_branch_prefix:{self.branch_prefix!r}")

        if not isinstance(self.base_branch, str) or not self.base_branch.strip():
            raise ValueError("empty_base_branch")
        if not _BRANCH_NAME_RE.match(self.base_branch):
            raise ValueError(f"invalid_base_branch:{self.base_branch!r}")

        if (
            isinstance(self.git_timeout_seconds, bool)
            or not isinstance(self.git_timeout_seconds, int)
            or self.git_timeout_seconds <= 0
        ):
            raise ValueError(
                f"invalid_git_timeout:{self.git_timeout_seconds!r}"
            )

        # Persist resolved paths.
        object.__setattr__(self, "main_repo_path", resolved_main)
        object.__setattr__(self, "worktree_root", resolved_root)
        object.__setattr__(self, "branch_prefix", self.branch_prefix.strip())
        object.__setattr__(self, "base_branch", self.base_branch.strip())


@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    branch: str
    path: Path
    created_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _TASK_ID_RE.match(self.task_id):
            raise ValueError(f"invalid_task_id:{self.task_id!r}")
        if not isinstance(self.branch, str) or not _BRANCH_NAME_RE.match(self.branch):
            raise ValueError(f"invalid_branch:{self.branch!r}")
        if not isinstance(self.path, Path):
            raise ValueError("path_must_be_path_object")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or self.created_at <= 0
        ):
            raise ValueError(f"invalid_created_at:{self.created_at!r}")


class SandboxWorkspace:
    """Manages worktrees rooted in `config.main_repo_path`.

    Thread-safety: this class is NOT safe for concurrent acquire of the
    same task_id. The bot's BackgroundTaskRunner enforces single-task
    semantics, so concurrent acquire is impossible in practice.
    """

    def __init__(
        self,
        config: SandboxConfig,
        *,
        runner: "_SubprocessRunner | None" = None,
    ) -> None:
        if not isinstance(config, SandboxConfig):
            raise ValueError(
                f"invalid_config_type:{type(config).__name__}"
            )
        self._cfg = config
        self._runner = runner if runner is not None else _DefaultSubprocessRunner()

    @property
    def config(self) -> SandboxConfig:
        return self._cfg

    def acquire(self, task_id: str) -> WorktreeHandle:
        """Create a new worktree for `task_id`. Branch = prefix + task_id.

        Raises:
          SandboxError("worktree_exists") if the path is already on disk.
          SandboxError("git_failed") if git itself fails.
          ValueError on bad task_id.
        """
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
            raise ValueError(f"invalid_task_id:{task_id!r}")

        branch = f"{self._cfg.branch_prefix}{task_id}"
        if not _BRANCH_NAME_RE.match(branch):
            raise ValueError(f"invalid_resulting_branch:{branch!r}")

        # Ensure the worktree-root directory exists.
        self._cfg.worktree_root.mkdir(parents=True, exist_ok=True)

        worktree_path = self._cfg.worktree_root / task_id
        if worktree_path.exists():
            raise SandboxError(
                "worktree_exists",
                f"path already on disk: {worktree_path}",
            )

        # `git worktree add -b <branch> <path> <base>`
        result = self._git(
            "worktree", "add",
            "-b", branch,
            str(worktree_path),
            self._cfg.base_branch,
        )
        if result.returncode != 0:
            raise SandboxError(
                "git_failed",
                f"git worktree add: {_excerpt(result.stderr)}",
            )

        return WorktreeHandle(
            task_id=task_id,
            branch=branch,
            path=worktree_path,
            created_at=time.time(),
        )

    def release(
        self,
        handle: WorktreeHandle,
        *,
        delete_branch: bool = False,
    ) -> None:
        """Remove the worktree directory and git's record of it.

        Idempotent: if the worktree is already gone, returns silently.
        If `delete_branch` is True, also deletes the local branch
        (use AFTER pushing to GitHub if you want to free space).
        """
        if not isinstance(handle, WorktreeHandle):
            raise ValueError(
                f"invalid_handle_type:{type(handle).__name__}"
            )

        # 1. git worktree remove (with --force in case branch is dirty).
        if handle.path.exists():
            result = self._git(
                "worktree", "remove", "--force", str(handle.path),
            )
            if result.returncode != 0:
                # Fall back to manual rm — git may have lost track of it.
                shutil.rmtree(handle.path, ignore_errors=True)

        # 2. Prune stale worktree records (cheap and idempotent).
        self._git("worktree", "prune")

        # 3. Optionally remove the local branch.
        if delete_branch:
            self._git("branch", "-D", handle.branch)

    def commit_in_worktree(
        self,
        handle: WorktreeHandle,
        *,
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """Stage all changes in the worktree and commit. Returns commit SHA.

        Raises SandboxError on validation failures or if there is nothing
        to commit (we don't want empty commits in the team's history).
        """
        if not isinstance(handle, WorktreeHandle):
            raise ValueError(
                f"invalid_handle_type:{type(handle).__name__}"
            )
        if not isinstance(message, str) or not message.strip():
            raise ValueError("empty_commit_message")
        if any(meta in message for meta in ("\x00",)):
            raise ValueError("invalid_commit_message_chars")
        if not isinstance(author_name, str) or not author_name.strip():
            raise ValueError("empty_author_name")
        if not isinstance(author_email, str) or "@" not in author_email:
            raise ValueError("invalid_author_email")
        if not handle.path.exists():
            raise SandboxError("worktree_missing", str(handle.path))

        # 1. Stage everything.
        result_add = self._git_in(handle.path, "add", "-A")
        if result_add.returncode != 0:
            raise SandboxError(
                "git_add_failed",
                _excerpt(result_add.stderr),
            )

        # 2. Check if there is anything staged.
        status = self._git_in(handle.path, "status", "--porcelain")
        if not status.stdout.strip():
            raise SandboxError(
                "nothing_to_commit",
                "no changes staged in worktree",
            )

        # 3. Commit with explicit author identity.
        commit_args = (
            "-c", f"user.name={author_name.strip()}",
            "-c", f"user.email={author_email.strip()}",
            "commit", "-m", message.strip(),
        )
        result_commit = self._git_in(handle.path, *commit_args)
        if result_commit.returncode != 0:
            raise SandboxError(
                "git_commit_failed",
                _excerpt(result_commit.stderr),
            )

        # 4. Resolve the new HEAD SHA.
        result_sha = self._git_in(handle.path, "rev-parse", "HEAD")
        if result_sha.returncode != 0:
            raise SandboxError(
                "git_rev_parse_failed",
                _excerpt(result_sha.stderr),
            )
        return result_sha.stdout.strip()

    def push_named_branch(
        self,
        branch_name: str,
        *,
        remote: str = "origin",
    ) -> None:
        """`git push <remote> <branch_name>` from main_repo (worktree NOT required).

        Use after worktree release: branch persists in main_repo so push works.
        Validates branch_name against _BRANCH_NAME_RE; raises SandboxError on
        git failure.

        Raises:
            ValueError: on invalid branch_name or remote name.
            SandboxError("git_push_failed"): when git returns non-zero.
        """
        if not isinstance(branch_name, str) or not _BRANCH_NAME_RE.match(branch_name):
            raise ValueError(f"invalid_branch:{branch_name!r}")
        if not isinstance(remote, str) or not _REMOTE_NAME_RE.match(remote):
            raise ValueError(f"invalid_remote_name:{remote!r}")
        result = self._git("push", remote, branch_name)
        if result.returncode != 0:
            raise SandboxError(
                "git_push_failed",
                _excerpt(result.stderr),
            )

    def push_branch_from_main(
        self,
        branch: str,
        *,
        remote: str = "origin",
    ) -> None:
        """DEPRECATED: use push_named_branch instead.

        Kept for backward compatibility with tests written against the 14c-1
        interface.  Will be removed in a future cleanup pass.
        """
        return self.push_named_branch(branch, remote=remote)

    def gh_pr_create(
        self,
        branch_name: str,
        *,
        title: str,
        body: str,
        base: str = "main",
    ) -> str:
        """Create a draft GitHub PR via the `gh` CLI from main_repo.

        Runs `gh pr create --base <base> --head <branch_name> --title "..." --body "..." --draft`
        from main_repo. The `gh` binary must be installed and authenticated
        (`gh auth status` should report authenticated).

        Args:
            branch_name: feature branch (must already be pushed to remote).
            title:       PR title (1-256 chars, no NULs).
            body:        PR body (any UTF-8 text, no NULs).
            base:        target branch (default "main").

        Returns:
            PR URL on success (parsed from `gh` stdout).

        Raises:
            ValueError: invalid args.
            SandboxError("gh_not_found"): `gh` binary missing.
            SandboxError("gh_pr_create_failed"): non-zero exit (auth issue,
                branch not on remote, network, etc.).
        """
        if not isinstance(branch_name, str) or not _BRANCH_NAME_RE.match(branch_name):
            raise ValueError(f"invalid_branch:{branch_name!r}")
        if not isinstance(base, str) or not _BRANCH_NAME_RE.match(base):
            raise ValueError(f"invalid_base:{base!r}")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("empty_title")
        if "\x00" in title:
            raise ValueError("invalid_title_chars")
        if len(title) > 256:
            raise ValueError(f"title_too_long:{len(title)}")
        if not isinstance(body, str):
            raise ValueError("body_must_be_str")
        if "\x00" in body:
            raise ValueError("invalid_body_chars")

        # Reject shell metas only in identifiers (branch/base) — they're
        # consumed as git refs and need to be strict. title/body are
        # freeform PR content (backticks for code formatting are common
        # in markdown) and pass through subprocess argv, not a shell, so
        # there's nothing to escape.
        for tok in (branch_name, base):
            for meta in _SHELL_META_TOKENS:
                if meta in tok:
                    raise ValueError(f"shell_meta_in_arg:{meta!r}")

        cmd = (
            "gh", "pr", "create",
            "--base", base,
            "--head", branch_name,
            "--title", title.strip(),
            "--body", body,
            "--draft",
        )
        env = _build_subprocess_env()
        try:
            result = self._runner.run(
                cmd=cmd,
                cwd=str(self._cfg.main_repo_path),
                env=env,
                timeout=self._cfg.git_timeout_seconds,
            )
        except Exception as exc:
            raise SandboxError("gh_subprocess_error", str(exc)) from exc

        # Subprocess runner returns rc=127 with "git_not_found"-style stderr
        # when the binary is missing; we surface that as gh_not_found.
        if result.returncode == 127 or "not found" in (result.stderr or "").lower():
            raise SandboxError("gh_not_found", _excerpt(result.stderr))
        if result.returncode != 0:
            raise SandboxError(
                "gh_pr_create_failed",
                _excerpt(result.stderr or result.stdout),
            )

        # `gh pr create` prints the PR URL on its own line. Pick the last URL.
        for line in reversed((result.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("https://") and "/pull/" in line:
                return line
        # Fallback: return whole stdout if URL not found (rare).
        return (result.stdout or "").strip()

    def push_branch(
        self,
        handle: WorktreeHandle,
        *,
        remote: str = "origin",
    ) -> None:
        """`git push <remote> <branch>` from the worktree.

        Caller must have ConfirmationGate-approved this — push to remote
        is in the ALWAYS_ASK category.
        """
        if not isinstance(handle, WorktreeHandle):
            raise ValueError(
                f"invalid_handle_type:{type(handle).__name__}"
            )
        if not isinstance(remote, str) or not _REMOTE_NAME_RE.match(remote):
            raise ValueError(f"invalid_remote_name:{remote!r}")
        if not handle.path.exists():
            raise SandboxError("worktree_missing", str(handle.path))

        result = self._git_in(handle.path, "push", remote, handle.branch)
        if result.returncode != 0:
            raise SandboxError(
                "git_push_failed",
                _excerpt(result.stderr),
            )

    def list_worktrees(self) -> tuple[str, ...]:
        """Returns a tuple of worktree paths git knows about (parsed from
        `git worktree list --porcelain`).
        """
        result = self._git("worktree", "list", "--porcelain")
        if result.returncode != 0:
            raise SandboxError(
                "git_worktree_list_failed",
                _excerpt(result.stderr),
            )
        paths: list[str] = []
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                paths.append(line[len("worktree ") :].strip())
        return tuple(paths)

    def cleanup_orphans(self) -> int:
        """Removes /tmp/aidt_worktrees/* directories that git no longer
        tracks. Returns count of removed directories.
        """
        if not self._cfg.worktree_root.exists():
            return 0
        tracked = set(self.list_worktrees())
        removed = 0
        for entry in self._cfg.worktree_root.iterdir():
            if not entry.is_dir():
                continue
            if str(entry.resolve()) in tracked:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        # Tell git to forget any stale worktree records.
        self._git("worktree", "prune")
        return removed

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> "_RunResult":
        """Run git in the main repo."""
        return self._run_git(self._cfg.main_repo_path, args)

    def _git_in(self, cwd: Path, *args: str) -> "_RunResult":
        """Run git in a specific cwd (a worktree)."""
        return self._run_git(cwd, args)

    def _run_git(self, cwd: Path, args: tuple[str, ...]) -> "_RunResult":
        for tok in args:
            if not isinstance(tok, str):
                raise ValueError("non_string_git_arg")
            for meta in _SHELL_META_TOKENS:
                if meta in tok:
                    raise ValueError(f"shell_meta_in_git_arg:{meta!r}")
        cmd = ("git", *args)
        env = _build_subprocess_env()
        return self._runner.run(
            cmd=cmd,
            cwd=str(cwd),
            env=env,
            timeout=self._cfg.git_timeout_seconds,
        )


# ---------------------------------------------------------------------------
# Subprocess plumbing — testable via injection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunResult:
    returncode: int
    stdout: str
    stderr: str


class _SubprocessRunner:
    """Protocol-style: tests can substitute their own fake."""

    def run(
        self,
        cmd: tuple[str, ...],
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> _RunResult:  # pragma: no cover — protocol
        raise NotImplementedError


class _DefaultSubprocessRunner(_SubprocessRunner):
    def run(
        self,
        cmd: tuple[str, ...],
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> _RunResult:
        try:
            proc = subprocess.run(
                list(cmd),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _RunResult(returncode=124, stdout="", stderr=f"timeout:{timeout}s")
        except FileNotFoundError as exc:
            return _RunResult(returncode=127, stdout="", stderr=f"git_not_found:{exc}")
        except OSError as exc:
            return _RunResult(returncode=126, stdout="", stderr=f"os_error:{exc}")
        return _RunResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


def _build_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(_BASE_ENV)
    for key in _PASSTHROUGH_ENV:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if extra:
        env.update(extra)
    return env


def _excerpt(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def to_dict_handles(handles: Iterable[WorktreeHandle]) -> list[dict]:
    """Helper for serialising handles to JSON-friendly dicts (logs / API)."""
    return [
        {
            "task_id": h.task_id,
            "branch": h.branch,
            "path": str(h.path),
            "created_at": h.created_at,
        }
        for h in handles
    ]
