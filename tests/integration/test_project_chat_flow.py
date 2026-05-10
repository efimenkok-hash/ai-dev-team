from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.agent_personas import default_registry
from core.bot_runner import build_command_registry
from core.project_chat_binding_service import ProjectChatBindingService
from core.project_context import ProjectContextResolver
from core.project_migration_service import ProjectMigrationService
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_runtime_router import ProjectRuntimeRouter
from core.project_summary_service import ProjectSummaryService
from core.sandbox_workspace import SandboxWorkspace
from core.state_db import StateDB
from core.task_history import TaskHistory, TaskSummary
from core.telegram_bridge import BridgeReply, IncomingMessage, OutgoingMessage, TelegramBridge

OWNER_ID = 777


def _make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": OWNER_ID,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _runtime_binding(repo_path: Path, **overrides: object) -> ProjectRuntimeBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / f"{repo_path.name}-worktrees",
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


def _chat_binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -100123450001,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _project_snapshot(
    repo_path: Path,
    *,
    project_id: str = "alpha_project",
    slug: str = "alpha-project",
    name: str = "Alpha Project",
    owner_user_id: int = OWNER_ID,
    chat_binding: ProjectChatBinding | None = None,
) -> ProjectSnapshot:
    return ProjectSnapshot(
        project=_project(
            project_id=project_id,
            slug=slug,
            name=name,
            owner_user_id=owner_user_id,
        ),
        policy=_policy(project_id=project_id),
        chat_binding=chat_binding,
        runtime_binding=_runtime_binding(
            repo_path,
            project_id=project_id,
            adapter_name=f"{project_id}_adapter",
        ),
    )


def _message(
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    text: str,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
    )


def _captured_send() -> tuple[Callable[[OutgoingMessage], None], list[OutgoingMessage]]:
    captured: list[OutgoingMessage] = []

    def _send(msg: OutgoingMessage) -> None:
        captured.append(msg)

    return _send, captured


@dataclass
class _Harness:
    bridge: TelegramBridge
    registry: ProjectRegistry
    runtime_router: ProjectRuntimeRouter
    task_history: TaskHistory | None
    captured: list[OutgoingMessage]
    task_calls: list[dict[str, object]]


def _build_harness(
    registry: ProjectRegistry,
    *,
    owner_user_ids: tuple[int, ...] = (OWNER_ID,),
    task_history: TaskHistory | None = None,
) -> _Harness:
    resolver = ProjectContextResolver(registry, owner_user_ids)
    binding_service = ProjectChatBindingService(registry, owner_user_ids)
    migration_service = ProjectMigrationService(
        registry,
        binding_service,
        owner_user_ids,
    )
    summary_service = ProjectSummaryService(
        registry,
        resolver,
        migration_service=migration_service,
    )
    runtime_router = ProjectRuntimeRouter(registry, None)
    commands = build_command_registry(
        default_registry(),
        task_history=task_history,
        runtime_router=runtime_router,
        project_chat_binding_service=binding_service,
        project_migration_service=migration_service,
        project_context_resolver=resolver,
        project_summary_service=summary_service,
    )
    send, captured = _captured_send()
    task_calls: list[dict[str, object]] = []

    def _task_handler(text: str, msg: IncomingMessage) -> BridgeReply:
        resolved = runtime_router.resolve_message_runtime(msg)
        task_calls.append(
            {
                "text": text,
                "message_project_id": msg.project_id,
                "message_project_context_source": msg.project_context_source,
                "resolved_project_id": resolved.snapshot.project.project_id,
                "runtime_source": resolved.source,
                "repo_path": resolved.sandbox.config.main_repo_path,
            }
        )
        return BridgeReply(
            persona_role="architect_agent",
            body=f"task routed to {resolved.snapshot.project.project_id}",
        )

    bridge = TelegramBridge(
        owner_chat_ids=frozenset(owner_user_ids),
        send=send,
        commands=commands,
        task_handler=_task_handler,
        project_context_resolver=resolver,
    )
    return _Harness(
        bridge=bridge,
        registry=registry,
        runtime_router=runtime_router,
        task_history=task_history,
        captured=captured,
        task_calls=task_calls,
    )


def _record_task(
    task_history: TaskHistory,
    *,
    task_id: str,
    project_id: str,
    branch: str,
    commit_sha: str = "0123456789abcdef0123456789abcdef01234567",
) -> None:
    task_history.record(
        TaskSummary(
            task_id=task_id,
            branch=branch,
            commit_sha=commit_sha,
            final_state="SUCCESS",
            failure_reason=None,
            tier_name="STANDARD",
            finished_at=1234567890.0,
            project_id=project_id,
        )
    )


def test_single_legacy_project_can_migrate_into_explicit_project_chat(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "legacy-alpha")
    registry.register_project(_project_snapshot(alpha_repo))
    harness = _build_harness(registry)

    pre_summary = harness.bridge.handle(
        _message(
            chat_id=-100123450901,
            user_id=OWNER_ID,
            message_id=1,
            text="/project",
        )
    )
    migrate = harness.bridge.handle(
        _message(
            chat_id=-100123450901,
            user_id=OWNER_ID,
            message_id=2,
            text="/projects migrate here",
        )
    )
    post_summary = harness.bridge.handle(
        _message(
            chat_id=-100123450901,
            user_id=OWNER_ID,
            message_id=3,
            text="/project",
        )
    )
    task = harness.bridge.handle(
        _message(
            chat_id=-100123450901,
            user_id=999,
            message_id=4,
            text="finish the migration task",
        )
    )
    second_migrate = harness.bridge.handle(
        _message(
            chat_id=-100123450901,
            user_id=OWNER_ID,
            message_id=5,
            text="/projects migrate here",
        )
    )

    assert pre_summary.handled is True
    assert pre_summary.reason == "command"
    assert "/projects migrate here" in harness.captured[0].text
    assert "ещё не создан" in harness.captured[0].text.lower()
    assert "этот чат привязан" not in harness.captured[0].text.lower()

    assert migrate.handled is True
    assert migrate.reason == "command"
    assert "explicit project chat" in harness.captured[1].text.lower()

    assert post_summary.handled is True
    assert post_summary.reason == "command"
    assert "explicit project chat" in harness.captured[2].text.lower()
    assert "alpha-project" in harness.captured[2].text

    assert task.handled is True
    assert task.reason == "task"
    assert harness.task_calls == [
        {
            "text": "finish the migration task",
            "message_project_id": "alpha_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "alpha_project",
            "runtime_source": "message_project_id",
            "repo_path": alpha_repo.resolve(),
        }
    ]

    assert second_migrate.handled is True
    assert second_migrate.reason == "command"
    assert "миграция не требуется" in harness.captured[4].text.lower()


def test_multi_project_group_chat_requires_explicit_bind_and_never_auto_selects(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "multi-alpha")
    beta_repo = _git_repo(tmp_path, "multi-beta")
    registry.register_project(_project_snapshot(alpha_repo))
    registry.register_project(
        _project_snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    harness = _build_harness(registry)

    pre_summary = harness.bridge.handle(
        _message(
            chat_id=-100123450902,
            user_id=OWNER_ID,
            message_id=1,
            text="/project",
        )
    )
    migrate = harness.bridge.handle(
        _message(
            chat_id=-100123450902,
            user_id=OWNER_ID,
            message_id=2,
            text="/projects migrate here",
        )
    )
    bind = harness.bridge.handle(
        _message(
            chat_id=-100123450902,
            user_id=OWNER_ID,
            message_id=3,
            text="/projects bind beta-project",
        )
    )
    post_summary = harness.bridge.handle(
        _message(
            chat_id=-100123450902,
            user_id=OWNER_ID,
            message_id=4,
            text="/project",
        )
    )
    task = harness.bridge.handle(
        _message(
            chat_id=-100123450902,
            user_id=999,
            message_id=5,
            text="run beta scoped task",
        )
    )

    assert pre_summary.handled is True
    assert pre_summary.reason == "command"
    assert "не определён" in harness.captured[0].text.lower()
    assert "/projects bind" in harness.captured[0].text
    assert "beta_project" not in harness.captured[0].text

    assert migrate.handled is True
    assert migrate.reason == "command"
    assert "/projects bind" in harness.captured[1].text

    assert bind.handled is True
    assert bind.reason == "command"
    assert "beta-project" in harness.captured[2].text

    assert post_summary.handled is True
    assert post_summary.reason == "command"
    assert "beta-project" in harness.captured[3].text
    assert "beta_project" in harness.captured[3].text
    assert "explicit project chat" in harness.captured[3].text.lower()

    assert task.handled is True
    assert task.reason == "task"
    assert harness.task_calls == [
        {
            "text": "run beta scoped task",
            "message_project_id": "beta_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "beta_project",
            "runtime_source": "message_project_id",
            "repo_path": beta_repo.resolve(),
        }
    ]


def test_two_bound_project_chats_remain_isolated_for_tasks_and_switch(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "isolated-alpha")
    beta_repo = _git_repo(tmp_path, "isolated-beta")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(
                project_id="alpha_project",
                chat_id=-100123450903,
            ),
        )
    )
    registry.register_project(
        _project_snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
            chat_binding=_chat_binding(
                project_id="beta_project",
                chat_id=-100123450904,
            ),
        )
    )
    harness = _build_harness(registry)

    result_a = harness.bridge.handle(
        _message(
            chat_id=-100123450903,
            user_id=111,
            message_id=1,
            text="alpha task 1",
        )
    )
    result_b = harness.bridge.handle(
        _message(
            chat_id=-100123450904,
            user_id=222,
            message_id=2,
            text="beta task 1",
        )
    )
    project_a = harness.bridge.handle(
        _message(
            chat_id=-100123450903,
            user_id=111,
            message_id=3,
            text="/project",
        )
    )
    project_b = harness.bridge.handle(
        _message(
            chat_id=-100123450904,
            user_id=222,
            message_id=4,
            text="/project",
        )
    )
    switch_a = harness.bridge.handle(
        _message(
            chat_id=-100123450903,
            user_id=111,
            message_id=5,
            text="/switch beta-project",
        )
    )
    task_after_switch_a = harness.bridge.handle(
        _message(
            chat_id=-100123450903,
            user_id=111,
            message_id=6,
            text="alpha task 2 after switch",
        )
    )
    switch_b = harness.bridge.handle(
        _message(
            chat_id=-100123450904,
            user_id=222,
            message_id=7,
            text="/switch alpha-project",
        )
    )
    task_after_switch_b = harness.bridge.handle(
        _message(
            chat_id=-100123450904,
            user_id=222,
            message_id=8,
            text="beta task 2 after switch",
        )
    )

    assert result_a.reason == "task"
    assert result_b.reason == "task"
    assert project_a.reason == "command"
    assert project_b.reason == "command"
    assert switch_a.reason == "command"
    assert task_after_switch_a.reason == "task"
    assert switch_b.reason == "command"
    assert task_after_switch_b.reason == "task"

    assert "alpha-project" in harness.captured[2].text
    assert "beta-project" in harness.captured[3].text
    assert "не используется" in harness.captured[4].text.lower()
    assert "не используется" in harness.captured[6].text.lower()

    assert harness.task_calls == [
        {
            "text": "alpha task 1",
            "message_project_id": "alpha_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "alpha_project",
            "runtime_source": "message_project_id",
            "repo_path": alpha_repo.resolve(),
        },
        {
            "text": "beta task 1",
            "message_project_id": "beta_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "beta_project",
            "runtime_source": "message_project_id",
            "repo_path": beta_repo.resolve(),
        },
        {
            "text": "alpha task 2 after switch",
            "message_project_id": "alpha_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "alpha_project",
            "runtime_source": "message_project_id",
            "repo_path": alpha_repo.resolve(),
        },
        {
            "text": "beta task 2 after switch",
            "message_project_id": "beta_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "beta_project",
            "runtime_source": "message_project_id",
            "repo_path": beta_repo.resolve(),
        },
    ]


def test_unbind_immediately_revokes_project_task_execution(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "unbind-alpha")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(chat_id=-100123450905),
        )
    )
    harness = _build_harness(registry)

    before = harness.bridge.handle(
        _message(
            chat_id=-100123450905,
            user_id=999,
            message_id=1,
            text="bound task before unbind",
        )
    )
    unbind = harness.bridge.handle(
        _message(
            chat_id=-100123450905,
            user_id=OWNER_ID,
            message_id=2,
            text="/projects unbind",
        )
    )
    summary = harness.bridge.handle(
        _message(
            chat_id=-100123450905,
            user_id=OWNER_ID,
            message_id=3,
            text="/project",
        )
    )
    blocked = harness.bridge.handle(
        _message(
            chat_id=-100123450905,
            user_id=999,
            message_id=4,
            text="task after unbind",
        )
    )

    assert before.handled is True
    assert before.reason == "task"
    assert unbind.handled is True
    assert unbind.reason == "command"
    assert "отвязан" in harness.captured[1].text.lower()

    assert summary.handled is True
    assert summary.reason == "command"
    assert "не определён" in harness.captured[2].text.lower()
    assert "/projects migrate here" in harness.captured[2].text

    assert blocked.handled is False
    assert blocked.reason == "project_context_missing"
    assert "ещё не привязан к проекту" in harness.captured[3].text.lower()
    assert harness.task_calls == [
        {
            "text": "bound task before unbind",
            "message_project_id": "alpha_project",
            "message_project_context_source": "bound_chat",
            "resolved_project_id": "alpha_project",
            "runtime_source": "message_project_id",
            "repo_path": alpha_repo.resolve(),
        }
    ]


def test_owner_dm_single_project_fallback_remains_operational(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "dm-single-alpha")
    registry.register_project(_project_snapshot(alpha_repo))
    harness = _build_harness(registry)

    summary = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=1,
            text="/project",
        )
    )
    first_task = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=2,
            text="owner dm task 1",
        )
    )
    switch = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=3,
            text="/switch alpha-project",
        )
    )
    second_task = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=4,
            text="owner dm task 2",
        )
    )

    assert summary.handled is True
    assert summary.reason == "command"
    assert "owner dm fallback" in harness.captured[0].text.lower()

    assert first_task.handled is True
    assert first_task.reason == "task"

    assert switch.handled is True
    assert switch.reason == "command"
    assert "отдельное переключение не требуется" in harness.captured[2].text.lower()

    assert second_task.handled is True
    assert second_task.reason == "task"
    assert harness.task_calls == [
        {
            "text": "owner dm task 1",
            "message_project_id": "alpha_project",
            "message_project_context_source": "owner_dm_single_project",
            "resolved_project_id": "alpha_project",
            "runtime_source": "message_project_id",
            "repo_path": alpha_repo.resolve(),
        },
        {
            "text": "owner dm task 2",
            "message_project_id": "alpha_project",
            "message_project_context_source": "owner_dm_single_project",
            "resolved_project_id": "alpha_project",
            "runtime_source": "message_project_id",
            "repo_path": alpha_repo.resolve(),
        },
    ]


def test_owner_dm_multi_project_never_auto_selects_runtime(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "dm-multi-alpha")
    beta_repo = _git_repo(tmp_path, "dm-multi-beta")
    registry.register_project(_project_snapshot(alpha_repo))
    registry.register_project(
        _project_snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    harness = _build_harness(registry)

    summary = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=1,
            text="/project",
        )
    )
    blocked_before_switch = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=2,
            text="owner dm task should block",
        )
    )
    switch = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=3,
            text="/switch beta-project",
        )
    )
    blocked_after_switch = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=4,
            text="owner dm task still blocked",
        )
    )
    migrate = harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=5,
            text="/projects migrate here",
        )
    )

    assert summary.handled is True
    assert summary.reason == "command"
    assert "явный project chat" in harness.captured[0].text.lower()

    assert blocked_before_switch.handled is False
    assert blocked_before_switch.reason == "project_context_missing"
    assert "нужен явный проектный чат" in harness.captured[1].text.lower()

    assert switch.handled is True
    assert switch.reason == "command"
    assert "не выбирает runtime-проект" in harness.captured[2].text.lower()

    assert blocked_after_switch.handled is False
    assert blocked_after_switch.reason == "project_context_missing"
    assert "нужен явный проектный чат" in harness.captured[3].text.lower()

    assert migrate.handled is True
    assert migrate.reason == "command"
    assert "group/supergroup" in harness.captured[4].text.lower()
    assert harness.task_calls == []


def test_push_and_pr_reject_cross_project_task_ids_after_chat_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "push-pr-alpha")
    beta_repo = _git_repo(tmp_path, "push-pr-beta")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(
                project_id="alpha_project",
                chat_id=-100123450906,
            ),
        )
    )
    registry.register_project(
        _project_snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
            chat_binding=_chat_binding(
                project_id="beta_project",
                chat_id=-100123450907,
            ),
        )
    )
    task_history = TaskHistory(state_db=db)
    _record_task(
        task_history,
        task_id="task-alpha-123",
        project_id="alpha_project",
        branch="feature/task-alpha-123",
    )
    harness = _build_harness(registry, task_history=task_history)

    push_calls: list[tuple[Path, str]] = []
    pr_calls: list[tuple[Path, str]] = []

    def _fake_push_named_branch(self: SandboxWorkspace, branch_name: str, *, remote: str = "origin") -> None:
        push_calls.append((self.config.main_repo_path, branch_name))

    def _fake_gh_pr_create(
        self: SandboxWorkspace,
        branch_name: str,
        *,
        title: str,
        body: str,
        base: str = "main",
    ) -> str:
        pr_calls.append((self.config.main_repo_path, branch_name))
        return "https://example.invalid/pr/123"

    monkeypatch.setattr(
        SandboxWorkspace,
        "push_named_branch",
        _fake_push_named_branch,
    )
    monkeypatch.setattr(
        SandboxWorkspace,
        "gh_pr_create",
        _fake_gh_pr_create,
    )

    push = harness.bridge.handle(
        _message(
            chat_id=-100123450907,
            user_id=222,
            message_id=1,
            text="/push task-alpha-123",
        )
    )
    pr = harness.bridge.handle(
        _message(
            chat_id=-100123450907,
            user_id=222,
            message_id=2,
            text="/pr task-alpha-123",
        )
    )

    assert push.handled is True
    assert push.reason == "command"
    assert "относится к другому проекту" in harness.captured[0].text.lower()
    assert "alpha_project" in harness.captured[0].text
    assert "beta_project" in harness.captured[0].text

    assert pr.handled is True
    assert pr.reason == "command"
    assert "относится к другому проекту" in harness.captured[1].text.lower()
    assert "alpha_project" in harness.captured[1].text
    assert "beta_project" in harness.captured[1].text

    assert push_calls == []
    assert pr_calls == []
