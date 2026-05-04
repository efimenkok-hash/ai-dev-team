import shutil
import subprocess
from pathlib import Path

import pytest

from core.git_integration import (
    CommitInfo,
    GitRepo,
    PullRequestDraft,
    make_pr_draft,
    validate_branch_name,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available in environment",
)


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _seed_repo(path: Path) -> GitRepo:
    repo = GitRepo(path)
    init = repo.init("main")
    assert init.ok, init.stderr
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    repo.stage(["README.md"])
    repo.commit("chore: initial commit")
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return _seed_repo(tmp_path / "work")


# ---------------------------------------------------------------------------
# branch name validator
# ---------------------------------------------------------------------------


def test_validate_branch_name_accepts_typical_names():
    for name in ["main", "feature/x", "release-1.2", "user/abc_def", "v1.0"]:
        validate_branch_name(name)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "-leading-dash",
        "/leading-slash",
        "trailing/",
        "has space",
        "with..dots",
        "tilde~bad",
        "caret^bad",
        "colon:bad",
        "lock.lock",
        "back\\slash",
        "ref@{0}",
        "non-ascii-русский",
    ],
)
def test_validate_branch_name_rejects(bad):
    with pytest.raises(ValueError):
        validate_branch_name(bad)


# ---------------------------------------------------------------------------
# init / lifecycle
# ---------------------------------------------------------------------------


def test_init_creates_repo_with_main(tmp_path: Path):
    repo = GitRepo(tmp_path / "fresh")
    assert not repo.is_initialized()
    result = repo.init("main")
    assert result.ok
    assert repo.is_initialized()


def test_init_idempotent(tmp_path: Path):
    repo = GitRepo(tmp_path / "fresh")
    repo.init("main")
    second = repo.init("main")
    assert second.ok
    assert "already_initialized" in second.stdout


def test_init_rejects_invalid_initial_branch(tmp_path: Path):
    repo = GitRepo(tmp_path / "x")
    with pytest.raises(ValueError):
        repo.init("not valid")


# ---------------------------------------------------------------------------
# stage / commit
# ---------------------------------------------------------------------------


def test_seed_repo_has_initial_commit(repo: GitRepo):
    sha = repo.head_sha()
    assert len(sha) == 40


def test_commit_returns_commit_info(repo: GitRepo):
    p = repo.path / "a.txt"
    p.write_text("alpha\n", encoding="utf-8")
    repo.stage(["a.txt"])
    info = repo.commit("feat: add a")
    assert isinstance(info, CommitInfo)
    assert info.subject == "feat: add a"
    assert len(info.sha) == 40


def test_commit_rejects_short_message(repo: GitRepo):
    with pytest.raises(ValueError, match="commit_message_too_short"):
        repo.commit("ok")


def test_commit_rejects_whitespace_message(repo: GitRepo):
    with pytest.raises(ValueError, match="commit_message_too_short"):
        repo.commit("    \n")


def test_stage_rejects_empty_paths(repo: GitRepo):
    with pytest.raises(ValueError, match="empty_paths"):
        repo.stage([])


def test_stage_rejects_path_escape(repo: GitRepo):
    with pytest.raises(ValueError, match="path_escapes_repo"):
        repo.stage(["../escape"])


def test_stage_rejects_absolute_outside_repo(repo: GitRepo, tmp_path: Path):
    foreign = tmp_path / "elsewhere.txt"
    foreign.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="path_outside_repo"):
        repo.stage([str(foreign)])


def test_stage_rejects_empty_path_entry(repo: GitRepo):
    with pytest.raises(ValueError, match="empty_path_entry"):
        repo.stage(["  "])


# ---------------------------------------------------------------------------
# branches
# ---------------------------------------------------------------------------


def test_create_branch_switches_head(repo: GitRepo):
    result = repo.create_branch("feature/x")
    assert result.ok, result.stderr
    assert repo.current_branch() == "feature/x"
    assert "feature/x" in repo.list_branches()
    assert "main" in repo.list_branches()


def test_create_branch_validates_name(repo: GitRepo):
    with pytest.raises(ValueError):
        repo.create_branch("bad name")


def test_checkout_back_to_main(repo: GitRepo):
    repo.create_branch("feature/y")
    repo.checkout("main")
    assert repo.current_branch() == "main"


def test_checkout_rejects_empty_ref(repo: GitRepo):
    with pytest.raises(ValueError, match="empty_ref"):
        repo.checkout("")


# ---------------------------------------------------------------------------
# diff / log
# ---------------------------------------------------------------------------


def test_diff_between_branches(repo: GitRepo):
    repo.create_branch("feature/diff")
    p = repo.path / "b.txt"
    p.write_text("beta\n", encoding="utf-8")
    repo.stage(["b.txt"])
    repo.commit("feat: add b")
    diff = repo.diff("main", "feature/diff")
    assert "+beta" in diff
    assert "b.txt" in diff


def test_log_lists_commits_between_refs(repo: GitRepo):
    repo.create_branch("feature/log")
    (repo.path / "c.txt").write_text("c\n", encoding="utf-8")
    repo.stage(["c.txt"])
    repo.commit("feat: add c")
    (repo.path / "d.txt").write_text("d\n", encoding="utf-8")
    repo.stage(["d.txt"])
    repo.commit("feat: add d")
    log = repo.log("main", "feature/log")
    subjects = [c.subject for c in log]
    assert subjects == ["feat: add d", "feat: add c"]
    for c in log:
        assert len(c.sha) == 40


# ---------------------------------------------------------------------------
# rollback safety
# ---------------------------------------------------------------------------


def test_reset_hard_blocked_on_main_without_force(repo: GitRepo):
    sha = repo.head_sha()
    with pytest.raises(RuntimeError, match="protected_branch_reset_blocked:main"):
        repo.reset_hard(sha)


def test_reset_hard_allowed_on_feature_branch(repo: GitRepo):
    repo.create_branch("feature/r")
    base_sha = repo.head_sha()
    (repo.path / "z.txt").write_text("z\n", encoding="utf-8")
    repo.stage(["z.txt"])
    repo.commit("feat: temp z")
    new_sha = repo.head_sha()
    assert new_sha != base_sha

    result = repo.reset_hard(base_sha)
    assert result.ok, result.stderr
    assert repo.head_sha() == base_sha
    assert not (repo.path / "z.txt").exists()


def test_reset_hard_force_required_explicitly(repo: GitRepo):
    sha = repo.head_sha()
    # Even providing force=True must work (used only by humans, never by agents).
    result = repo.reset_hard(sha, force=True)
    assert result.ok


def test_reset_hard_rejects_empty_ref(repo: GitRepo):
    with pytest.raises(ValueError, match="empty_ref"):
        repo.reset_hard("")


# ---------------------------------------------------------------------------
# pull request draft
# ---------------------------------------------------------------------------


def test_make_pr_draft_returns_frozen_dataclass(repo: GitRepo):
    repo.create_branch("feature/pr")
    (repo.path / "x.txt").write_text("x\n", encoding="utf-8")
    repo.stage(["x.txt"])
    repo.commit("feat: add x")
    draft = make_pr_draft(
        repo,
        base="main",
        head="feature/pr",
        title="Add x",
        body="Summary of what changed.",
    )
    assert isinstance(draft, PullRequestDraft)
    with pytest.raises(Exception):
        draft.title = "tampered"


def test_make_pr_draft_includes_diff_and_commit_subjects(repo: GitRepo):
    repo.create_branch("feature/pr2")
    (repo.path / "y.txt").write_text("y\n", encoding="utf-8")
    repo.stage(["y.txt"])
    repo.commit("feat: add y")
    draft = make_pr_draft(repo, "main", "feature/pr2", "Add y", "Why y matters")
    assert "+y" in draft.diff
    assert any(c.subject == "feat: add y" for c in draft.commits)
    assert "feat: add y" in draft.body


def test_make_pr_draft_validates_branches(repo: GitRepo):
    with pytest.raises(ValueError):
        make_pr_draft(repo, "bad name", "main", "title here", "body")
    with pytest.raises(ValueError):
        make_pr_draft(repo, "main", "also bad", "title here", "body")


def test_make_pr_draft_rejects_short_title(repo: GitRepo):
    with pytest.raises(ValueError, match="pr_title_too_short"):
        make_pr_draft(repo, "main", "main", "ab", "body")


def test_make_pr_draft_rejects_invalid_body(repo: GitRepo):
    with pytest.raises(ValueError, match="pr_body_invalid"):
        make_pr_draft(repo, "main", "main", "title here", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# subprocess safety
# ---------------------------------------------------------------------------


def test_git_runs_with_devnull_stdin_no_prompt(repo: GitRepo, monkeypatch):
    captured = {}

    real_run = subprocess.run

    def spy(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["stdin"] = kwargs.get("stdin")
        captured["env"] = dict(kwargs.get("env") or {})
        captured["timeout"] = kwargs.get("timeout")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    repo.head_sha()
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["env"].get("GIT_TERMINAL_PROMPT") == "0"
    assert captured["timeout"] == 30
    # Ensure no forbidden flags were passed.
    forbidden = {"--force", "--no-verify", "--no-edit", "-i", "--no-gpg-sign"}
    assert not (forbidden & set(captured["cmd"]))


def test_status_porcelain_returns_string(repo: GitRepo):
    out = repo.status_porcelain()
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# integration with patcher / contracts (already-written modules)
# ---------------------------------------------------------------------------


def test_full_cycle_branch_change_commit_diff_rollback(repo: GitRepo):
    base_sha = repo.head_sha()

    branch = "feature/full-cycle"
    repo.create_branch(branch)

    target = repo.path / "core_change.py"
    target.write_text("VERSION = '1.0.0'\n", encoding="utf-8")
    repo.stage(["core_change.py"])
    info = repo.commit("feat: introduce core_change")
    assert info.subject == "feat: introduce core_change"

    diff = repo.diff("main", branch)
    assert "core_change.py" in diff

    draft = make_pr_draft(repo, "main", branch, "Introduce core_change", "Why")
    assert draft.head == branch
    assert draft.base == "main"
    assert any(c.subject == "feat: introduce core_change" for c in draft.commits)

    # rollback feature branch to base
    repo.reset_hard(base_sha)
    assert repo.head_sha() == base_sha
    assert not target.exists()
