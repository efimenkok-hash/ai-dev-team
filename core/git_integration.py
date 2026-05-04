"""
core/git_integration.py

Local-only safe wrapper over the `git` CLI. Implements Step 9 of the
ULTRA spec: branches, commits, PR draft, rollback. No network operations:
push/fetch/clone are deliberately not exposed.

CONTRACTS:
1. Every subprocess call runs with cwd=repo_path, stdin=DEVNULL, no shell,
   timeout=30s, deterministic env (LANG=C, GIT_TERMINAL_PROMPT=0).
2. Forbidden flags are never emitted by this module:
   --force, --no-verify, --no-edit, -i, --no-gpg-sign.
3. Branch names pass validate_branch_name() before any git invocation.
4. reset_hard on main/master requires force=True; otherwise -> RuntimeError.
5. stage() rejects empty list, '..' in path, and absolute paths outside repo.
6. commit() rejects messages shorter than 3 chars after strip.
7. make_pr_draft() is pure: it only reads git history and assembles a
   markdown body. No GitHub API call.
"""

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_PROTECTED_BRANCHES = frozenset({"main", "master"})

_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

_FORBIDDEN_BRANCH_SUBSTRINGS = ("..", "@{", "\\")

_FORBIDDEN_FLAGS = frozenset({
    "--force",
    "-f",
    "--no-verify",
    "--no-edit",
    "-i",
    "--interactive",
    "--no-gpg-sign",
})

_GIT_TIMEOUT_SECONDS = 30

_GIT_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str


@dataclass(frozen=True)
class PullRequestDraft:
    base: str
    head: str
    title: str
    body: str
    diff: str
    commits: tuple


def validate_branch_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("empty_branch_name")
    if not _BRANCH_NAME_RE.match(name):
        raise ValueError(f"invalid_branch_name:{name}")
    for sub in _FORBIDDEN_BRANCH_SUBSTRINGS:
        if sub in name:
            raise ValueError(f"forbidden_branch_substring:{sub}")
    if name.endswith("/") or name.endswith(".lock"):
        raise ValueError(f"invalid_branch_suffix:{name}")


def _scrub_args(args: Sequence[str]) -> None:
    for arg in args:
        if arg in _FORBIDDEN_FLAGS:
            raise RuntimeError(f"forbidden_git_flag:{arg}")


class GitRepo:
    def __init__(self, path) -> None:
        self.path = Path(path).resolve()

    # ----- core invoker ----------------------------------------------------

    def _git(self, *args: str) -> GitResult:
        _scrub_args(args)
        cmd = ["git", *args]
        proc = subprocess.run(
            cmd,
            cwd=str(self.path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=_GIT_ENV,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        return GitResult(
            ok=(proc.returncode == 0),
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )

    # ----- repo lifecycle --------------------------------------------------

    def is_initialized(self) -> bool:
        if not self.path.exists():
            return False
        return (self.path / ".git").exists()

    def init(self, initial_branch: str = "main") -> GitResult:
        validate_branch_name(initial_branch)
        if not self.path.exists():
            self.path.mkdir(parents=True, exist_ok=True)
        if self.is_initialized():
            return GitResult(ok=True, stdout="already_initialized", stderr="", returncode=0)
        result = self._git("init", "-b", initial_branch)
        if not result.ok:
            return result
        # Repo-local identity so subsequent commits succeed without global config.
        self._git("config", "user.name", "AI Dev Team")
        self._git("config", "user.email", "ai-dev-team@local")
        self._git("config", "commit.gpgsign", "false")
        return result

    # ----- branches --------------------------------------------------------

    def current_branch(self) -> str:
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if not result.ok:
            raise RuntimeError(f"git_current_branch_failed:{result.stderr.strip()}")
        return result.stdout.strip()

    def list_branches(self) -> list[str]:
        result = self._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        if not result.ok:
            raise RuntimeError(f"git_list_branches_failed:{result.stderr.strip()}")
        return [line for line in result.stdout.splitlines() if line]

    def create_branch(self, name: str, base: str | None = None) -> GitResult:
        validate_branch_name(name)
        args = ["checkout", "-b", name]
        if base is not None:
            validate_branch_name(base)
            args.append(base)
        return self._git(*args)

    def checkout(self, ref: str) -> GitResult:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("empty_ref")
        return self._git("checkout", ref)

    # ----- working tree ----------------------------------------------------

    def status_porcelain(self) -> str:
        result = self._git("status", "--porcelain=v1")
        if not result.ok:
            raise RuntimeError(f"git_status_failed:{result.stderr.strip()}")
        return result.stdout

    def stage(self, paths: list[str]) -> GitResult:
        if not paths:
            raise ValueError("empty_paths")
        repo_root = self.path
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("empty_path_entry")
            if ".." in Path(raw).parts:
                raise ValueError(f"path_escapes_repo:{raw}")
            candidate = (repo_root / raw).resolve()
            try:
                candidate.relative_to(repo_root)
            except ValueError as exc:
                raise ValueError(f"path_outside_repo:{raw}") from exc
        return self._git("add", "--", *paths)

    def commit(
        self,
        message: str,
        author_name: str = "AI Dev Team",
        author_email: str = "ai-dev-team@local",
    ) -> CommitInfo:
        if not isinstance(message, str) or len(message.strip()) < 3:
            raise ValueError("commit_message_too_short")
        if not author_name or not author_email:
            raise ValueError("missing_author")
        author = f"{author_name} <{author_email}>"
        result = self._git(
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit",
            "-m", message,
            "--author", author,
        )
        if not result.ok:
            raise RuntimeError(f"git_commit_failed:{result.stderr.strip()}")
        sha = self.head_sha()
        return CommitInfo(sha=sha, subject=message.splitlines()[0].strip())

    def head_sha(self) -> str:
        result = self._git("rev-parse", "HEAD")
        if not result.ok:
            raise RuntimeError(f"git_head_sha_failed:{result.stderr.strip()}")
        return result.stdout.strip()

    # ----- diff / log / rollback ------------------------------------------

    def diff(self, ref_a: str, ref_b: str) -> str:
        if not ref_a or not ref_b:
            raise ValueError("empty_ref")
        result = self._git("diff", f"{ref_a}..{ref_b}")
        if not result.ok:
            raise RuntimeError(f"git_diff_failed:{result.stderr.strip()}")
        return result.stdout

    def log(self, ref_a: str, ref_b: str) -> list[CommitInfo]:
        if not ref_a or not ref_b:
            raise ValueError("empty_ref")
        result = self._git(
            "log",
            f"{ref_a}..{ref_b}",
            "--pretty=format:%H%x09%s",
        )
        if not result.ok:
            raise RuntimeError(f"git_log_failed:{result.stderr.strip()}")
        commits: list[CommitInfo] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, subject = line.partition("\t")
            commits.append(CommitInfo(sha=sha.strip(), subject=subject.strip()))
        return commits

    def reset_hard(self, ref: str, *, force: bool = False) -> GitResult:
        if not ref or not ref.strip():
            raise ValueError("empty_ref")
        branch = self.current_branch()
        if branch in _PROTECTED_BRANCHES and not force:
            raise RuntimeError(f"protected_branch_reset_blocked:{branch}")
        return self._git("reset", "--hard", ref)


def make_pr_draft(
    repo: GitRepo,
    base: str,
    head: str,
    title: str,
    body: str,
) -> PullRequestDraft:
    validate_branch_name(base)
    validate_branch_name(head)
    if not isinstance(title, str) or len(title.strip()) < 3:
        raise ValueError("pr_title_too_short")
    if body is None or not isinstance(body, str):
        raise ValueError("pr_body_invalid")

    diff_text = repo.diff(base, head)
    commits = tuple(repo.log(base, head))

    commits_block = "\n".join(
        f"- {c.sha[:8]} {c.subject}" for c in commits
    ) or "_(no commits)_"

    rendered_body = (
        f"{body.strip()}\n\n"
        f"## Commits\n{commits_block}\n"
    )
    return PullRequestDraft(
        base=base,
        head=head,
        title=title.strip(),
        body=rendered_body,
        diff=diff_text,
        commits=commits,
    )
