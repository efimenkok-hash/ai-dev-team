"""Tests for core.state_db."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from core.agent_bus_models import AgentMessage, ProjectThread
from core.agent_dm_models import (
    DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    AgentDmMessage,
    AgentDmSession,
)
from core.agent_owner_notifications import AgentOwnerNotification
from core.hire_approval import PendingHireRequest
from core.project_models import (
    Project,
    ProjectChatBinding,
    ProjectMembership,
    ProjectPolicy,
)
from core.project_runtime import ProjectRuntimeBinding
from core.project_team_state import ProjectSpecialistRoster
from core.state_db import StateDB
from core.task_history import TaskSummary


def _summary(
    task_id: str = "task-001",
    branch: str = "feature/task-001",
    commit_sha: str | None = "abc123def456789",
    final_state: str = "SUCCESS",
    failure_reason: str | None = None,
    tier_name: str = "ECONOMY",
    finished_at: float | None = None,
    project_id: str | None = None,
) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        branch=branch,
        commit_sha=commit_sha,
        final_state=final_state,
        failure_reason=failure_reason,
        tier_name=tier_name,
        finished_at=finished_at if finished_at is not None else time.time(),
        project_id=project_id,
    )


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": 101,
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


def _pending_hire_request(**overrides: object) -> PendingHireRequest:
    data: dict[str, object] = {
        "request_id": "hire-1000-abcd1234",
        "project_id": "alpha_project",
        "specialist_role": "security_agent",
        "reason": "Auth and secrets are in scope.",
        "source": "logical_hiring_pm_hint",
        "status": "pending",
        "created_at": 1000.0,
    }
    data.update(overrides)
    return PendingHireRequest(**data)


def _membership(**overrides: object) -> ProjectMembership:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "member_id": "coordinator_01",
        "member_type": "agent",
        "role_name": "coordinator_agent",
        "status": "active",
    }
    data.update(overrides)
    return ProjectMembership(**data)


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_id": -1001234567890,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _runtime_binding(repo_path: Path, **overrides: object) -> ProjectRuntimeBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / "worktrees",
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (),
        "commands": (),
        "forbidden_paths": ("secrets/",),
        "forbidden_tokens": ("API_KEY",),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


def _make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _agent_dm_session(**overrides: object) -> AgentDmSession:
    data: dict[str, object] = {
        "owner_user_id": 101,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "dm_chat_id": 101,
        "chat_provider": "telegram",
        "status": "active",
        "created_at": 1000.0,
        "last_interaction_at": 1005.0,
    }
    data.update(overrides)
    return AgentDmSession(**data)


def _agent_dm_message(**overrides: object) -> AgentDmMessage:
    data: dict[str, object] = {
        "owner_user_id": 101,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "sender_kind": "owner",
        "sender_role": "owner",
        "body": "Need a first draft",
        "created_at": 1001.0,
    }
    data.update(overrides)
    return AgentDmMessage(**data)


def _agent_owner_notification(**overrides: object) -> AgentOwnerNotification:
    data: dict[str, object] = {
        "notification_id": None,
        "owner_user_id": 101,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "body": "Need owner review",
        "chat_provider": "telegram",
        "status": "queued",
        "created_at": 1002.0,
        "delivered_at": None,
    }
    data.update(overrides)
    return AgentOwnerNotification(**data)


def _project_thread(**overrides: object) -> ProjectThread:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "opened_by_role": "coordinator_agent",
        "status": "open",
        "created_at": 1000.0,
        "last_message_at": 1000.0,
        "task_id": None,
    }
    data.update(overrides)
    return ProjectThread(**data)


def _agent_bus_message(**overrides: object) -> AgentMessage:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "message_id": "msg_000001",
        "sender_role": "coordinator_agent",
        "recipient_role": "writer_agent",
        "message_kind": "request",
        "body": "Need a first draft",
        "created_at": 1001.0,
        "in_reply_to": None,
    }
    data.update(overrides)
    return AgentMessage(**data)


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        str(row[0])
        for row in rows
        if not str(row[0]).startswith("sqlite_")
    }


def _table_columns(path: Path, table_name: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    finally:
        conn.close()
    return [str(row[1]) for row in rows]


def _build_v1_db(path: Path, *, with_schema_meta: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if with_schema_meta:
            conn.execute(
                "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')"
            )
        conn.execute(
            """
            CREATE TABLE tier_sessions (
                chat_id INTEGER PRIMARY KEY,
                tier_name TEXT NOT NULL,
                last_changed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_history (
                task_id TEXT PRIMARY KEY,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                final_state TEXT NOT NULL,
                failure_reason TEXT,
                tier_name TEXT NOT NULL,
                finished_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE budget (
                chat_id INTEGER PRIMARY KEY,
                usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tier_sessions(chat_id, tier_name, last_changed_at)
            VALUES (1, 'STANDARD', 1234.5)
            """
        )
        conn.execute(
            """
            INSERT INTO task_history(
                task_id, branch, commit_sha, final_state,
                failure_reason, tier_name, finished_at
            )
            VALUES (
                'task-v1',
                'feature/task-v1',
                'deadbeef',
                'SUCCESS',
                NULL,
                'STANDARD',
                2222.5
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget(chat_id, usd)
            VALUES (1, 7.5)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v2_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')"
        )
        conn.execute(
            """
            CREATE TABLE tier_sessions (
                chat_id INTEGER PRIMARY KEY,
                active_tier TEXT NOT NULL,
                last_changed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                final_state TEXT NOT NULL,
                failure_reason TEXT,
                tier_name TEXT NOT NULL,
                finished_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE budget (
                chat_id INTEGER PRIMARY KEY,
                usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tier_sessions(chat_id, active_tier, last_changed_at)
            VALUES (1, 'STANDARD', 1234.5)
            """
        )
        conn.execute(
            """
            INSERT INTO task_history(
                task_id, branch, commit_sha, final_state,
                failure_reason, tier_name, finished_at
            )
            VALUES (
                'task-v2',
                'feature/task-v2',
                'feedface',
                'SUCCESS',
                NULL,
                'PREMIUM',
                3333.5
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget(chat_id, usd)
            VALUES (1, 8.25)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v3_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')"
        )
        conn.execute(
            """
            CREATE TABLE tier_sessions (
                chat_id INTEGER PRIMARY KEY,
                active_tier TEXT NOT NULL,
                last_changed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                final_state TEXT NOT NULL,
                failure_reason TEXT,
                tier_name TEXT NOT NULL,
                finished_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE budget (
                chat_id INTEGER PRIMARY KEY,
                usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_policies (
                project_id TEXT PRIMARY KEY,
                allow_hiring INTEGER NOT NULL,
                allow_agent_dm INTEGER NOT NULL,
                require_owner_approval_for_hires INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_members (
                project_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                member_type TEXT NOT NULL,
                role_name TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(project_id, member_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_chat_bindings (
                project_id TEXT PRIMARY KEY,
                chat_provider TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                UNIQUE(chat_provider, chat_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tier_sessions(chat_id, active_tier, last_changed_at)
            VALUES (1, 'STANDARD', 1234.5)
            """
        )
        conn.execute(
            """
            INSERT INTO task_history(
                task_id, branch, commit_sha, final_state,
                failure_reason, tier_name, finished_at
            )
            VALUES (
                'task-v3',
                'feature/task-v3',
                'cafebabe',
                'SUCCESS',
                NULL,
                'STANDARD',
                4444.5
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget(chat_id, usd)
            VALUES (1, 9.5)
            """
        )
        conn.execute(
            """
            INSERT INTO projects(
                project_id, slug, name, description, owner_user_id, status
            )
            VALUES (
                'alpha_project',
                'alpha-project',
                'Alpha Project',
                'Primary AI Office project.',
                101,
                'active'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_policies(
                project_id, allow_hiring, allow_agent_dm,
                require_owner_approval_for_hires
            )
            VALUES ('alpha_project', 1, 0, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO project_members(
                project_id, member_id, member_type, role_name, status
            )
            VALUES (
                'alpha_project',
                'coordinator_01',
                'agent',
                'coordinator_agent',
                'active'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_chat_bindings(project_id, chat_provider, chat_id)
            VALUES ('alpha_project', 'telegram', -1001234567890)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v4_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '4')"
        )
        conn.execute(
            """
            CREATE TABLE tier_sessions (
                chat_id INTEGER PRIMARY KEY,
                active_tier TEXT NOT NULL,
                last_changed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                final_state TEXT NOT NULL,
                failure_reason TEXT,
                tier_name TEXT NOT NULL,
                finished_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE budget (
                chat_id INTEGER PRIMARY KEY,
                usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_policies (
                project_id TEXT PRIMARY KEY,
                allow_hiring INTEGER NOT NULL,
                allow_agent_dm INTEGER NOT NULL,
                require_owner_approval_for_hires INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_members (
                project_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                member_type TEXT NOT NULL,
                role_name TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(project_id, member_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_chat_bindings (
                project_id TEXT PRIMARY KEY,
                chat_provider TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                UNIQUE(chat_provider, chat_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_runtime_bindings (
                project_id TEXT PRIMARY KEY,
                adapter_name TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                worktree_root TEXT,
                base_branch TEXT NOT NULL,
                branch_prefix TEXT NOT NULL,
                language TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                commands_json TEXT NOT NULL,
                forbidden_paths_json TEXT NOT NULL,
                forbidden_tokens_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tier_sessions(chat_id, active_tier, last_changed_at)
            VALUES (1, 'STANDARD', 1234.5)
            """
        )
        conn.execute(
            """
            INSERT INTO task_history(
                task_id, branch, commit_sha, final_state,
                failure_reason, tier_name, finished_at
            )
            VALUES (
                'task-v4',
                'feature/task-v4',
                'abbaabba',
                'SUCCESS',
                NULL,
                'STANDARD',
                5555.5
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget(chat_id, usd)
            VALUES (1, 10.5)
            """
        )
        conn.execute(
            """
            INSERT INTO projects(
                project_id, slug, name, description, owner_user_id, status
            )
            VALUES (
                'alpha_project',
                'alpha-project',
                'Alpha Project',
                'Primary AI Office project.',
                101,
                'active'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v5_db(path: Path, repo_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '5')"
        )
        conn.execute(
            """
            CREATE TABLE tier_sessions (
                chat_id INTEGER PRIMARY KEY,
                active_tier TEXT NOT NULL,
                last_changed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                final_state TEXT NOT NULL,
                failure_reason TEXT,
                tier_name TEXT NOT NULL,
                finished_at REAL NOT NULL,
                project_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE budget (
                chat_id INTEGER PRIMARY KEY,
                usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_policies (
                project_id TEXT PRIMARY KEY,
                allow_hiring INTEGER NOT NULL,
                allow_agent_dm INTEGER NOT NULL,
                require_owner_approval_for_hires INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_members (
                project_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                member_type TEXT NOT NULL,
                role_name TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(project_id, member_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_chat_bindings (
                project_id TEXT PRIMARY KEY,
                chat_provider TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                UNIQUE(chat_provider, chat_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE project_runtime_bindings (
                project_id TEXT PRIMARY KEY,
                adapter_name TEXT NOT NULL,
                repo_path TEXT NOT NULL,
                worktree_root TEXT,
                base_branch TEXT NOT NULL,
                branch_prefix TEXT NOT NULL,
                language TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                commands_json TEXT NOT NULL,
                forbidden_paths_json TEXT NOT NULL,
                forbidden_tokens_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tier_sessions(chat_id, active_tier, last_changed_at)
            VALUES (1, 'STANDARD', 1234.5)
            """
        )
        conn.execute(
            """
            INSERT INTO task_history(
                task_id, branch, commit_sha, final_state,
                failure_reason, tier_name, finished_at, project_id
            )
            VALUES (
                'task-v5',
                'feature/task-v5',
                'f00dbabe',
                'SUCCESS',
                NULL,
                'PREMIUM',
                6666.5,
                'alpha_project'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget(chat_id, usd)
            VALUES (1, 11.5)
            """
        )
        conn.execute(
            """
            INSERT INTO projects(
                project_id, slug, name, description, owner_user_id, status
            )
            VALUES (
                'alpha_project',
                'alpha-project',
                'Alpha Project',
                'Primary AI Office project.',
                101,
                'active'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_policies(
                project_id, allow_hiring, allow_agent_dm,
                require_owner_approval_for_hires
            )
            VALUES ('alpha_project', 1, 0, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO project_members(
                project_id, member_id, member_type, role_name, status
            )
            VALUES (
                'alpha_project',
                'coordinator_01',
                'agent',
                'coordinator_agent',
                'active'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_chat_bindings(project_id, chat_provider, chat_id)
            VALUES ('alpha_project', 'telegram', -1001234567890)
            """
        )
        conn.execute(
            """
            INSERT INTO project_runtime_bindings(
                project_id, adapter_name, repo_path, worktree_root, base_branch,
                branch_prefix, language, rules_json, commands_json,
                forbidden_paths_json, forbidden_tokens_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alpha_project",
                "alpha_adapter",
                str(repo_path),
                str(repo_path.parent / "worktrees"),
                "main",
                "feature/",
                "python",
                json.dumps([]),
                json.dumps([]),
                json.dumps(["secrets/"]),
                json.dumps(["API_KEY"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _build_v6_db(path: Path, repo_path: Path) -> None:
    _build_v5_db(path, repo_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE agent_dm_sessions (
                owner_user_id INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                agent_role TEXT NOT NULL,
                thread_bot_role TEXT NOT NULL,
                dm_chat_id INTEGER NOT NULL,
                chat_provider TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_interaction_at REAL NOT NULL,
                PRIMARY KEY(owner_user_id, project_id, agent_role)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_dm_sessions(
                owner_user_id,
                project_id,
                agent_role,
                thread_bot_role,
                dm_chat_id,
                chat_provider,
                status,
                created_at,
                last_interaction_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "alpha_project",
                "writer_agent",
                "writer_agent",
                101,
                "telegram",
                "active",
                1000.0,
                1005.0,
            ),
        )
        conn.execute(
            """
            UPDATE schema_meta
            SET value = '6'
            WHERE key = 'schema_version'
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v7_db(path: Path, repo_path: Path) -> None:
    _build_v6_db(path, repo_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE agent_dm_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                agent_role TEXT NOT NULL,
                sender_kind TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX idx_agent_dm_messages_transcript_id_desc
            ON agent_dm_messages(owner_user_id, project_id, agent_role, id DESC)
            """
        )
        conn.execute(
            """
            INSERT INTO agent_dm_messages(
                owner_user_id,
                project_id,
                agent_role,
                sender_kind,
                sender_role,
                body,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "alpha_project",
                "writer_agent",
                "owner",
                "owner",
                "Need a draft",
                1001.0,
            ),
        )
        conn.execute(
            """
            UPDATE schema_meta
            SET value = '7'
            WHERE key = 'schema_version'
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v8_db(path: Path, repo_path: Path) -> None:
    _build_v7_db(path, repo_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE agent_owner_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                agent_role TEXT NOT NULL,
                thread_bot_role TEXT NOT NULL,
                body TEXT NOT NULL,
                chat_provider TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                delivered_at REAL NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX idx_agent_owner_notifications_queue
            ON agent_owner_notifications(
                owner_user_id,
                project_id,
                agent_role,
                thread_bot_role,
                status,
                id ASC
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_owner_notifications(
                owner_user_id,
                project_id,
                agent_role,
                thread_bot_role,
                body,
                chat_provider,
                status,
                created_at,
                delivered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "alpha_project",
                "writer_agent",
                "writer_agent",
                "Need owner review",
                "telegram",
                "queued",
                1002.0,
                None,
            ),
        )
        conn.execute(
            """
            UPDATE schema_meta
            SET value = '8'
            WHERE key = 'schema_version'
            """
        )
        conn.commit()
    finally:
        conn.close()


def _build_v9_db(path: Path, repo_path: Path) -> None:
    _build_v8_db(path, repo_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE project_threads (
                project_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                opened_by_role TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_message_at REAL NOT NULL,
                task_id TEXT NULL,
                PRIMARY KEY(project_id, thread_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE agent_bus_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                recipient_role TEXT NOT NULL,
                message_kind TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at REAL NOT NULL,
                in_reply_to_project_id TEXT NULL,
                in_reply_to_thread_id TEXT NULL,
                in_reply_to_message_id TEXT NULL,
                UNIQUE(project_id, thread_id, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX idx_project_threads_project_activity
            ON project_threads(project_id, last_message_at DESC, thread_id ASC)
            """
        )
        conn.execute(
            """
            CREATE INDEX idx_agent_bus_messages_thread_id_asc
            ON agent_bus_messages(project_id, thread_id, id ASC)
            """
        )
        conn.execute(
            """
            CREATE INDEX idx_agent_bus_messages_inbox_id_asc
            ON agent_bus_messages(project_id, recipient_role, id ASC)
            """
        )
        conn.execute(
            """
            INSERT INTO project_threads(
                project_id,
                thread_id,
                opened_by_role,
                status,
                created_at,
                last_message_at,
                task_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alpha_project",
                "thread_000001",
                "coordinator_agent",
                "open",
                1000.0,
                1001.0,
                "task-v9",
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_bus_messages(
                project_id,
                thread_id,
                message_id,
                sender_role,
                recipient_role,
                message_kind,
                body,
                created_at,
                in_reply_to_project_id,
                in_reply_to_thread_id,
                in_reply_to_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alpha_project",
                "thread_000001",
                "msg_000001",
                "coordinator_agent",
                "writer_agent",
                "request",
                "Need a first draft",
                1001.0,
                None,
                None,
                None,
            ),
        )
        conn.execute(
            """
            UPDATE schema_meta
            SET value = '9'
            WHERE key = 'schema_version'
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Construction / schema
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_path():
    with pytest.raises(ValueError, match="invalid_path_type"):
        StateDB("not-a-path")  # type: ignore[arg-type]


def test_constructor_rejects_empty_path():
    with pytest.raises(ValueError, match="empty_path"):
        StateDB(Path(""))


def test_constructor_creates_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "deeper" / "state.db"
    StateDB(db_path)
    assert db_path.parent.exists()


def test_constructor_creates_database_file(tmp_path: Path):
    db_path = tmp_path / "state.db"
    StateDB(db_path)
    assert db_path.exists()


def test_schema_version_is_current(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.schema_version() == 11


def test_fresh_schema_creates_project_tables(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.schema_version() == 11
    assert _table_names(db.path) >= {
        "schema_meta",
        "tier_sessions",
        "task_history",
        "budget",
        "projects",
        "project_policies",
        "project_members",
        "project_specialist_roster",
        "project_hire_requests",
        "project_chat_bindings",
        "project_runtime_bindings",
        "agent_dm_sessions",
        "agent_dm_messages",
        "agent_owner_notifications",
        "project_threads",
        "agent_bus_messages",
    }
    assert _table_columns(db.path, "task_history") == [
        "id",
        "task_id",
        "branch",
        "commit_sha",
        "final_state",
        "failure_reason",
        "tier_name",
        "finished_at",
        "project_id",
    ]
    assert _table_columns(db.path, "projects") == [
        "project_id",
        "slug",
        "name",
        "description",
        "owner_user_id",
        "status",
    ]
    assert _table_columns(db.path, "project_policies") == [
        "project_id",
        "allow_hiring",
        "allow_agent_dm",
        "require_owner_approval_for_hires",
    ]
    assert _table_columns(db.path, "project_members") == [
        "project_id",
        "member_id",
        "member_type",
        "role_name",
        "status",
    ]
    assert _table_columns(db.path, "project_specialist_roster") == [
        "project_id",
        "specialist_role",
    ]
    assert _table_columns(db.path, "project_chat_bindings") == [
        "project_id",
        "chat_provider",
        "chat_id",
    ]
    assert _table_columns(db.path, "project_runtime_bindings") == [
        "project_id",
        "adapter_name",
        "repo_path",
        "worktree_root",
        "base_branch",
        "branch_prefix",
        "language",
        "rules_json",
        "commands_json",
        "forbidden_paths_json",
        "forbidden_tokens_json",
    ]
    assert _table_columns(db.path, "agent_dm_sessions") == [
        "owner_user_id",
        "project_id",
        "agent_role",
        "thread_bot_role",
        "dm_chat_id",
        "chat_provider",
        "status",
        "created_at",
        "last_interaction_at",
    ]
    assert _table_columns(db.path, "agent_dm_messages") == [
        "id",
        "owner_user_id",
        "project_id",
        "agent_role",
        "sender_kind",
        "sender_role",
        "body",
        "created_at",
    ]
    assert _table_columns(db.path, "agent_owner_notifications") == [
        "id",
        "owner_user_id",
        "project_id",
        "agent_role",
        "thread_bot_role",
        "body",
        "chat_provider",
        "status",
        "created_at",
        "delivered_at",
    ]
    assert _table_columns(db.path, "project_threads") == [
        "project_id",
        "thread_id",
        "opened_by_role",
        "status",
        "created_at",
        "last_message_at",
        "task_id",
    ]
    assert _table_columns(db.path, "agent_bus_messages") == [
        "id",
        "project_id",
        "thread_id",
        "message_id",
        "sender_role",
        "recipient_role",
        "message_kind",
        "body",
        "created_at",
        "in_reply_to_project_id",
        "in_reply_to_thread_id",
        "in_reply_to_message_id",
    ]


def test_wal_mode_enabled(tmp_path: Path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db.path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_path_property_returns_normalized_path(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = StateDB(db_path)
    assert db.path == db_path.expanduser()


def test_bus_guardrails_reject_invalid_types_and_missing_scope(tmp_path: Path):
    db = _make_db(tmp_path)

    with pytest.raises(ValueError, match="invalid_project_thread_type:str"):
        db.upsert_project_thread("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_agent_bus_message_type:str"):
        db.insert_agent_bus_message("bad")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.upsert_project_thread(_project_thread())

    db.upsert_project(_project())

    with pytest.raises(
        ValueError,
        match="unknown_project_thread:alpha_project:thread_000001",
    ):
        db.insert_agent_bus_message(_agent_bus_message())


def test_bus_guardrails_reject_duplicate_message_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_project_thread())
    db.insert_agent_bus_message(_agent_bus_message())

    with pytest.raises(
        ValueError,
        match="duplicate_agent_bus_message:alpha_project:thread_000001:msg_000001",
    ):
        db.insert_agent_bus_message(_agent_bus_message())


def test_constructor_rejects_partial_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "broken.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tier_sessions (chat_id INTEGER PRIMARY KEY, tier_name TEXT, last_changed_at REAL)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="partial_legacy_schema"):
        StateDB(db_path)


def test_constructor_rejects_future_schema_version(tmp_path: Path):
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '999')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="unsupported_schema_version:999"):
        StateDB(db_path)


def test_constructor_rejects_invalid_schema_version_value(tmp_path: Path):
    db_path = tmp_path / "invalid.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', 'oops')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="invalid_schema_version"):
        StateDB(db_path)


# ---------------------------------------------------------------------------
# Tier sessions
# ---------------------------------------------------------------------------


def test_get_tier_returns_none_for_unknown_chat(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_tier(1) is None


def test_set_tier_and_get_tier_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(42, "STANDARD")
    assert db.get_tier(42) == "STANDARD"


def test_list_tiers_returns_sorted_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(7, "STANDARD", last_changed_at=7.0)
    db.set_tier(3, "ECONOMY", last_changed_at=3.0)
    db.set_tier(9, "PREMIUM", last_changed_at=9.0)

    assert db.list_tiers() == (
        (3, "ECONOMY", 3.0),
        (7, "STANDARD", 7.0),
        (9, "PREMIUM", 9.0),
    )


def test_set_tier_strips_whitespace(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(5, "  PREMIUM  ")
    assert db.get_tier(5) == "PREMIUM"


def test_set_tier_overwrites_existing_value(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(8, "ECONOMY")
    db.set_tier(8, "STANDARD")
    assert db.get_tier(8) == "STANDARD"


def test_reset_tier_deletes_entry(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(9, "ECONOMY")
    db.reset_tier(9)
    assert db.get_tier(9) is None


def test_reset_tier_is_idempotent(tmp_path: Path):
    db = _make_db(tmp_path)
    db.reset_tier(123)
    assert db.get_tier(123) is None


@pytest.mark.parametrize("bad", [0, True, "1"])
def test_tier_methods_reject_invalid_chat_id(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.get_tier(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.set_tier(bad, "STANDARD")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.reset_tier(bad)  # type: ignore[arg-type]


def test_tier_methods_allow_negative_transport_chat_id(tmp_path: Path):
    db = _make_db(tmp_path)

    db.set_tier(-1001234567890, "STANDARD", last_changed_at=7.0)

    assert db.get_tier(-1001234567890) == "STANDARD"


@pytest.mark.parametrize("bad", ["", "   ", 123])
def test_set_tier_rejects_invalid_tier_name(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="empty_tier_name"):
        db.set_tier(1, bad)  # type: ignore[arg-type]


def test_concurrent_tier_reads_and_writes_do_not_crash(tmp_path: Path):
    db = _make_db(tmp_path)
    errors: list[Exception] = []

    def writer(chat_id: int) -> None:
        try:
            for _ in range(20):
                db.set_tier(chat_id, "STANDARD")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader(chat_id: int) -> None:
        try:
            for _ in range(20):
                _ = db.get_tier(chat_id)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 6)]
    threads += [threading.Thread(target=reader, args=(i,)) for i in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    for chat_id in range(1, 6):
        assert db.get_tier(chat_id) == "STANDARD"


# ---------------------------------------------------------------------------
# Task history
# ---------------------------------------------------------------------------


def test_record_task_and_get_task_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    summary = _summary(task_id="task-a")
    db.record_task(summary)
    assert db.get_task("task-a") == summary


def test_record_task_round_trip_preserves_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    summary = _summary(task_id="task-project", project_id="alpha_project")

    db.record_task(summary)

    loaded = db.get_task("task-project")
    assert loaded is not None
    assert loaded.project_id == "alpha_project"


def test_get_task_returns_none_for_unknown_task(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_task("missing-task") is None


def test_record_task_allows_duplicate_task_id_and_gets_newest(tmp_path: Path):
    db = _make_db(tmp_path)
    older = _summary(task_id="task-x", final_state="FAIL")
    newer = _summary(task_id="task-x", final_state="SUCCESS")
    db.record_task(older)
    db.record_task(newer)
    assert db.get_task("task-x") == newer


def test_recent_tasks_returns_newest_last(tmp_path: Path):
    db = _make_db(tmp_path)
    for i in range(4):
        db.record_task(_summary(task_id=f"task-{i}"))
    recent = db.recent_tasks(3)
    assert [item.task_id for item in recent] == ["task-1", "task-2", "task-3"]


def test_recent_tasks_returns_all_when_fewer_than_requested(tmp_path: Path):
    db = _make_db(tmp_path)
    db.record_task(_summary(task_id="task-1"))
    db.record_task(_summary(task_id="task-2"))
    assert len(db.recent_tasks(10)) == 2


def test_recent_tasks_empty_when_no_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.recent_tasks(5) == []


def test_recent_tasks_keeps_duplicate_entries_in_audit_log(tmp_path: Path):
    db = _make_db(tmp_path)
    db.record_task(_summary(task_id="task-z", final_state="FAIL"))
    db.record_task(_summary(task_id="task-z", final_state="SUCCESS"))
    recent = db.recent_tasks(2)
    assert len(recent) == 2
    assert [item.final_state for item in recent] == ["FAIL", "SUCCESS"]


def test_record_task_rejects_non_summary(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_summary_type"):
        db.record_task("bad")  # type: ignore[arg-type]


def test_trim_task_history_keeps_newest_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    for i in range(4):
        db.record_task(_summary(task_id=f"task-{i}"))

    db.trim_task_history(2)

    recent = db.recent_tasks(10)
    assert [item.task_id for item in recent] == ["task-2", "task-3"]
    assert db.get_task("task-0") is None
    assert db.get_task("task-3") is not None


@pytest.mark.parametrize("bad", ["", "   ", 123])
def test_get_task_rejects_invalid_task_id(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="empty_task_id"):
        db.get_task(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_recent_tasks_rejects_invalid_n(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_n"):
        db.recent_tasks(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_trim_task_history_rejects_invalid_max_entries(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_max_entries"):
        db.trim_task_history(bad)  # type: ignore[arg-type]


def test_concurrent_task_history_reads_and_writes_do_not_crash(tmp_path: Path):
    db = _make_db(tmp_path)
    errors: list[Exception] = []

    def writer(worker_id: int) -> None:
        try:
            for i in range(10):
                db.record_task(_summary(task_id=f"task-{worker_id}-{i}"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(20):
                _ = db.recent_tasks(5)
                _ = db.get_task("task-0-0")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    threads += [threading.Thread(target=reader) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert db.get_task("task-0-0") is not None


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_get_budget_returns_none_for_unknown_chat(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_budget(1) is None


def test_set_budget_and_get_budget_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_budget(7, 12.5)
    assert db.get_budget(7) == pytest.approx(12.5)


def test_set_budget_overwrites_existing_value(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_budget(3, 5.0)
    db.set_budget(3, 7.25)
    assert db.get_budget(3) == pytest.approx(7.25)


def test_set_budget_allows_zero(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_budget(11, 0.0)
    assert db.get_budget(11) == pytest.approx(0.0)


@pytest.mark.parametrize("bad", [0, True, "1"])
def test_budget_methods_reject_invalid_chat_id(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.get_budget(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.set_budget(bad, 1.0)  # type: ignore[arg-type]


def test_budget_methods_allow_negative_transport_chat_id(tmp_path: Path):
    db = _make_db(tmp_path)

    db.set_budget(-1001234567890, 12.5)

    assert db.get_budget(-1001234567890) == pytest.approx(12.5)


@pytest.mark.parametrize("bad", [-1.0, True, "5"])
def test_set_budget_rejects_invalid_amount(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_budget"):
        db.set_budget(1, bad)  # type: ignore[arg-type]


def test_concurrent_budget_reads_and_writes_do_not_crash(tmp_path: Path):
    db = _make_db(tmp_path)
    errors: list[Exception] = []

    def writer(chat_id: int) -> None:
        try:
            for i in range(20):
                db.set_budget(chat_id, float(i))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader(chat_id: int) -> None:
        try:
            for _ in range(20):
                _ = db.get_budget(chat_id)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(1,))]
    threads += [threading.Thread(target=reader, args=(1,)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert db.get_budget(1) == pytest.approx(19.0)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_upsert_project_and_get_project_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    project = _project()

    db.upsert_project(project)

    assert db.get_project("alpha_project") == project


def test_get_project_returns_none_for_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_project("missing_project") is None


def test_get_project_by_slug_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    project = _project(slug="alpha-project")

    db.upsert_project(project)

    assert db.get_project_by_slug("  Alpha-Project  ") == project


def test_upsert_project_overwrites_existing_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    updated = _project(name="Alpha Project v2", description="Updated description.")

    db.upsert_project(updated)

    assert db.get_project("alpha_project") == updated


def test_list_projects_returns_sorted_by_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(project_id="zeta_project", slug="zeta-project"))
    db.upsert_project(_project(project_id="alpha_project", slug="alpha-project"))
    db.upsert_project(_project(project_id="beta_project", slug="beta-project"))

    assert [project.project_id for project in db.list_projects()] == [
        "alpha_project",
        "beta_project",
        "zeta_project",
    ]


def test_upsert_project_rejects_duplicate_slug_for_other_project(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(project_id="alpha_project", slug="shared-project"))

    with pytest.raises(ValueError, match="project_slug_already_exists:shared-project"):
        db.upsert_project(_project(project_id="beta_project", slug="shared-project"))


def test_upsert_project_rejects_non_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_type"):
        db.upsert_project("bad")  # type: ignore[arg-type]


def test_get_project_rejects_empty_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="empty_project_id"):
        db.get_project("  ")


def test_get_project_rejects_invalid_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_project("bad-id")


def test_get_project_by_slug_rejects_empty_slug(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="empty_slug"):
        db.get_project_by_slug(" ")


def test_get_project_by_slug_rejects_invalid_slug(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_slug"):
        db.get_project_by_slug("bad_slug")


# ---------------------------------------------------------------------------
# Project policies
# ---------------------------------------------------------------------------


def test_project_policy_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    policy = _policy(allow_hiring=False, allow_agent_dm=True)

    db.set_project_policy(policy)

    assert db.get_project_policy("alpha_project") == policy


def test_project_policy_flags_restore_as_bool(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.set_project_policy(
        _policy(
            allow_hiring=False,
            allow_agent_dm=True,
            require_owner_approval_for_hires=False,
        )
    )

    policy = db.get_project_policy("alpha_project")

    assert policy is not None
    assert isinstance(policy.allow_hiring, bool)
    assert isinstance(policy.allow_agent_dm, bool)
    assert isinstance(policy.require_owner_approval_for_hires, bool)
    assert policy.allow_hiring is False
    assert policy.allow_agent_dm is True
    assert policy.require_owner_approval_for_hires is False


def test_get_project_policy_returns_none_for_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_project_policy("missing_project") is None


def test_set_project_policy_rejects_non_policy(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_policy_type"):
        db.set_project_policy("bad")  # type: ignore[arg-type]


def test_set_project_policy_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.set_project_policy(_policy())


def test_get_project_policy_rejects_invalid_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_project_policy("bad-id")


# ---------------------------------------------------------------------------
# Project memberships
# ---------------------------------------------------------------------------


def test_project_membership_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    membership = _membership()

    db.upsert_project_membership(membership)

    assert db.get_project_membership("alpha_project", "coordinator_01") == membership


def test_list_project_memberships_returns_sorted_by_member_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_membership(_membership(member_id="writer_01", role_name="writer_agent"))
    db.upsert_project_membership(_membership(member_id="architect_01", role_name="architect_agent"))
    db.upsert_project_membership(_membership(member_id="coordinator_01", role_name="coordinator_agent"))

    assert [item.member_id for item in db.list_project_memberships("alpha_project")] == [
        "architect_01",
        "coordinator_01",
        "writer_01",
    ]


def test_member_id_is_unique_within_project_via_upsert(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_membership(_membership(status="active"))
    db.upsert_project_membership(
        _membership(
            status="inactive",
            role_name="writer_agent",
            member_type="human",
        )
    )

    memberships = db.list_project_memberships("alpha_project")

    assert len(memberships) == 1
    assert memberships[0].member_id == "coordinator_01"
    assert memberships[0].status == "inactive"
    assert memberships[0].role_name == "writer_agent"
    assert memberships[0].member_type == "human"


def test_same_member_id_can_exist_in_different_projects(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(project_id="alpha_project", slug="alpha-project"))
    db.upsert_project(_project(project_id="beta_project", slug="beta-project"))
    db.upsert_project_membership(_membership(project_id="alpha_project", member_id="shared_member"))
    db.upsert_project_membership(_membership(project_id="beta_project", member_id="shared_member"))

    alpha = db.get_project_membership("alpha_project", "shared_member")
    beta = db.get_project_membership("beta_project", "shared_member")

    assert alpha is not None
    assert beta is not None
    assert alpha.project_id == "alpha_project"
    assert beta.project_id == "beta_project"


def test_member_id_is_not_treated_as_chat_or_user_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_membership(_membership(member_id="member_101"))

    with pytest.raises(ValueError, match="empty_member_id"):
        db.get_project_membership("alpha_project", 101)  # type: ignore[arg-type]


def test_get_project_membership_returns_none_for_unknown_member(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    assert db.get_project_membership("alpha_project", "missing_member") is None


def test_upsert_project_membership_rejects_non_membership(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_membership_type"):
        db.upsert_project_membership("bad")  # type: ignore[arg-type]


def test_upsert_project_membership_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.upsert_project_membership(_membership())


def test_membership_methods_reject_invalid_project_or_member_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_project_membership("bad-id", "member_01")
    with pytest.raises(ValueError, match="invalid_member_id"):
        db.get_project_membership("alpha_project", "bad-id")
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.list_project_memberships("bad-id")


# ---------------------------------------------------------------------------
# Project specialist roster
# ---------------------------------------------------------------------------


def test_project_specialist_roster_round_trip_with_empty_default(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    assert db.get_project_specialist_roster("alpha_project") == (
        ProjectSpecialistRoster(
            project_id="alpha_project",
            specialist_roles=(),
        )
    )


def test_add_project_specialist_persists_and_orders_roles(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    db.add_project_specialist("alpha_project", "data_agent")
    db.add_project_specialist("alpha_project", "security_agent")

    assert db.list_project_specialists("alpha_project") == (
        "security_agent",
        "data_agent",
    )


def test_remove_project_specialist_persists(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.add_project_specialist("alpha_project", "security_agent")

    db.remove_project_specialist("alpha_project", "security_agent")

    assert db.list_project_specialists("alpha_project") == ()


def test_project_specialist_roster_persists_across_db_instances(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.add_project_specialist("alpha_project", "security_agent")
    db.add_project_specialist("alpha_project", "devops_agent")

    reopened = StateDB(db.path)

    assert reopened.get_project_specialist_roster("alpha_project") == (
        ProjectSpecialistRoster(
            project_id="alpha_project",
            specialist_roles=("security_agent", "devops_agent"),
        )
    )


def test_project_specialist_roster_is_isolated_per_project(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(project_id="alpha_project", slug="alpha-project"))
    db.upsert_project(_project(project_id="beta_project", slug="beta-project"))
    db.add_project_specialist("alpha_project", "security_agent")

    assert db.list_project_specialists("alpha_project") == ("security_agent",)
    assert db.list_project_specialists("beta_project") == ()


def test_add_project_specialist_rejects_duplicate_assignment(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.add_project_specialist("alpha_project", "security_agent")

    with pytest.raises(
        ValueError,
        match="duplicate_project_specialist:alpha_project:security_agent",
    ):
        db.add_project_specialist("alpha_project", "security_agent")


def test_remove_project_specialist_rejects_absent_assignment(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    with pytest.raises(
        ValueError,
        match="unknown_project_specialist:alpha_project:security_agent",
    ):
        db.remove_project_specialist("alpha_project", "security_agent")


@pytest.mark.parametrize("role", ("writer_agent", "coordinator_agent", "ghost_agent"))
def test_project_specialist_methods_reject_non_specialist_roles(
    tmp_path: Path,
    role: str,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    with pytest.raises(ValueError, match=fr"unknown_specialist_role:{role}"):
        db.add_project_specialist("alpha_project", role)


def test_project_specialist_methods_reject_invalid_project_id(tmp_path: Path):
    db = _make_db(tmp_path)

    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_project_specialist_roster("bad-id")


def test_create_hire_request_round_trip_and_list_pending(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    created = db.create_hire_request(_pending_hire_request())

    assert created.status == "pending"
    assert db.get_hire_request(created.request_id) == created
    assert db.list_pending_hire_requests("alpha_project") == (created,)


def test_create_hire_request_dedupes_identical_pending_request(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    first = db.create_hire_request(_pending_hire_request())
    second = db.create_hire_request(
        _pending_hire_request(request_id="hire-1001-efef5678")
    )

    assert second == first
    assert len(db.list_pending_hire_requests("alpha_project")) == 1


def test_mark_hire_request_approved_adds_specialist_and_clears_pending(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    created = db.create_hire_request(_pending_hire_request())

    approved = db.mark_hire_request_approved(created.request_id, 101)

    assert approved.status == "approved"
    assert approved.decided_by_user_id == 101
    assert db.list_project_specialists("alpha_project") == ("security_agent",)
    assert db.list_pending_hire_requests("alpha_project") == ()


def test_mark_hire_request_rejected_keeps_roster_unchanged(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    created = db.create_hire_request(_pending_hire_request())

    rejected = db.mark_hire_request_rejected(created.request_id, 101)

    assert rejected.status == "rejected"
    assert db.list_project_specialists("alpha_project") == ()
    assert db.list_pending_hire_requests("alpha_project") == ()


def test_mark_hire_request_approved_returns_existing_request_for_repeat_call(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    created = db.create_hire_request(_pending_hire_request())
    db.mark_hire_request_approved(created.request_id, 101)

    approved = db.mark_hire_request_approved(created.request_id, 101)

    assert approved.status == "approved"
    assert db.list_project_specialists("alpha_project") == ("security_agent",)


def test_hire_request_methods_reject_invalid_public_input(tmp_path: Path):
    db = _make_db(tmp_path)

    with pytest.raises(ValueError, match="invalid_hire_request_id"):
        db.get_hire_request("bad id")
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.list_pending_hire_requests("bad-id")
    with pytest.raises(ValueError, match="invalid_hire_approval_actor_user_id"):
        db.mark_hire_request_approved("hire-123-aaaa", 0)


# ---------------------------------------------------------------------------
# Project chat bindings
# ---------------------------------------------------------------------------


def test_project_chat_binding_round_trip_with_negative_telegram_chat_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    binding = _binding(chat_id=-1009876543210)

    db.bind_project_chat(binding)

    assert db.get_project_chat_binding("alpha_project") == binding


def test_get_project_for_chat_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    binding = _binding(chat_id=-1001234567890)
    db.bind_project_chat(binding)

    assert db.get_project_for_chat("telegram", -1001234567890) == binding


def test_project_chat_binding_is_one_to_one(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(project_id="alpha_project", slug="alpha-project"))
    db.upsert_project(_project(project_id="beta_project", slug="beta-project"))
    db.bind_project_chat(_binding(project_id="alpha_project", chat_id=-100123))

    with pytest.raises(ValueError, match="chat_binding_conflict:telegram:-100123"):
        db.bind_project_chat(_binding(project_id="beta_project", chat_id=-100123))


def test_bind_project_chat_rejects_non_binding(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_chat_binding_type"):
        db.bind_project_chat("bad")  # type: ignore[arg-type]


def test_bind_project_chat_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.bind_project_chat(_binding())


def test_get_project_chat_binding_returns_none_for_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_project_chat_binding("missing_project") is None


def test_get_project_for_chat_returns_none_for_unknown_chat(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_project_for_chat("telegram", -1001) is None


def test_chat_binding_methods_reject_invalid_project_provider_and_chat_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_project_chat_binding("bad-id")
    with pytest.raises(ValueError, match="invalid_chat_provider"):
        db.get_project_for_chat("discord", -1001)
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.get_project_for_chat("telegram", 0)


# ---------------------------------------------------------------------------
# Project runtime bindings
# ---------------------------------------------------------------------------


def test_project_runtime_binding_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    repo = _git_repo(tmp_path)
    db.upsert_project(_project())
    binding = _runtime_binding(repo)

    db.upsert_project_runtime_binding(binding)

    assert db.get_project_runtime_binding("alpha_project") == binding


def test_get_project_runtime_binding_returns_none_for_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_project_runtime_binding("missing_project") is None


def test_upsert_project_runtime_binding_overwrites_existing_value(tmp_path: Path):
    db = _make_db(tmp_path)
    repo = _git_repo(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_runtime_binding(_runtime_binding(repo))
    updated = _runtime_binding(
        repo,
        adapter_name="beta_adapter",
        base_branch="develop",
        branch_prefix="task/",
    )

    db.upsert_project_runtime_binding(updated)

    assert db.get_project_runtime_binding("alpha_project") == updated


def test_upsert_project_runtime_binding_rejects_non_binding(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_runtime_binding_type"):
        db.upsert_project_runtime_binding("bad")  # type: ignore[arg-type]


def test_upsert_project_runtime_binding_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.upsert_project_runtime_binding(_runtime_binding(_git_repo(tmp_path)))


def test_get_project_runtime_binding_rejects_invalid_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_project_runtime_binding("bad-id")


# ---------------------------------------------------------------------------
# Agent DM sessions
# ---------------------------------------------------------------------------


def test_agent_dm_session_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    session = _agent_dm_session()

    db.upsert_agent_dm_session(session)

    assert db.get_agent_dm_session(101, "alpha_project", "writer_agent") == session


def test_list_agent_dm_sessions_for_owner_returns_deterministic_order(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(project_id="alpha_project", slug="alpha-project"))
    db.upsert_project(_project(project_id="beta_project", slug="beta-project"))
    db.upsert_agent_dm_session(
        _agent_dm_session(
            project_id="beta_project",
            agent_role="reviewer_agent",
            thread_bot_role="reviewer_agent",
        )
    )
    db.upsert_agent_dm_session(
        _agent_dm_session(
            project_id="alpha_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
        )
    )
    db.upsert_agent_dm_session(
        _agent_dm_session(
            project_id="alpha_project",
            agent_role="architect_agent",
            thread_bot_role="architect_agent",
        )
    )

    sessions = db.list_agent_dm_sessions_for_owner(101)

    assert tuple(
        (session.project_id, session.agent_role) for session in sessions
    ) == (
        ("alpha_project", "architect_agent"),
        ("alpha_project", "writer_agent"),
        ("beta_project", "reviewer_agent"),
    )


def test_upsert_agent_dm_session_overwrites_existing_row(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())
    updated = _agent_dm_session(
        thread_bot_role="reviewer_agent",
        status="closed",
        created_at=2000.0,
        last_interaction_at=2005.0,
    )

    db.upsert_agent_dm_session(updated)

    assert db.get_agent_dm_session(101, "alpha_project", "writer_agent") == updated


def test_get_agent_dm_session_returns_none_for_missing_session(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    assert db.get_agent_dm_session(101, "alpha_project", "writer_agent") is None


def test_upsert_agent_dm_session_rejects_non_session(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_agent_dm_session_type:str"):
        db.upsert_agent_dm_session("bad")  # type: ignore[arg-type]


def test_upsert_agent_dm_session_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.upsert_agent_dm_session(_agent_dm_session())


def test_upsert_agent_dm_session_rejects_owner_project_mismatch(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project(owner_user_id=999))

    with pytest.raises(
        ValueError,
        match="agent_dm_session_owner_project_mismatch:101!=999",
    ):
        db.upsert_agent_dm_session(_agent_dm_session())


def test_get_agent_dm_session_rejects_invalid_args(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        db.get_agent_dm_session(0, "alpha_project", "writer_agent")
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.get_agent_dm_session(101, "bad-id", "writer_agent")
    with pytest.raises(ValueError, match="invalid_agent_role"):
        db.get_agent_dm_session(101, "alpha_project", "writer-agent")


def test_list_agent_dm_sessions_for_owner_rejects_invalid_owner_id(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        db.list_agent_dm_sessions_for_owner(0)


# ---------------------------------------------------------------------------
# Agent owner notifications
# ---------------------------------------------------------------------------


def test_insert_agent_owner_notification_round_trip_and_mark_delivered(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())

    inserted = db.insert_agent_owner_notification(_agent_owner_notification())

    assert inserted.notification_id is not None
    queued = db.list_queued_agent_owner_notifications(
        101,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    )
    assert queued == (inserted,)

    delivered = db.mark_agent_owner_notification_delivered(
        inserted.notification_id,
        delivered_at=1003.0,
    )

    assert delivered.status == "delivered"
    assert delivered.delivered_at == 1003.0
    assert db.list_queued_agent_owner_notifications(
        101,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    ) == ()


def test_list_queued_agent_owner_notifications_returns_oldest_first(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())

    first = db.insert_agent_owner_notification(
        _agent_owner_notification(body="First", created_at=1001.0)
    )
    second = db.insert_agent_owner_notification(
        _agent_owner_notification(body="Second", created_at=1002.0)
    )

    queued = db.list_queued_agent_owner_notifications(
        101,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    )

    assert queued == (first, second)


def test_insert_agent_owner_notification_rejects_non_notification(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_agent_owner_notification_type:str"):
        db.insert_agent_owner_notification("bad")  # type: ignore[arg-type]


def test_insert_agent_owner_notification_rejects_persisted_id(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    with pytest.raises(ValueError, match="notification_id_must_be_none_for_insert"):
        db.insert_agent_owner_notification(
            _agent_owner_notification(notification_id=1)
        )


def test_insert_agent_owner_notification_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.insert_agent_owner_notification(_agent_owner_notification())


def test_insert_agent_owner_notification_rejects_owner_project_mismatch(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project(owner_user_id=999))
    with pytest.raises(
        ValueError,
        match="agent_owner_notification_owner_project_mismatch:101!=999",
    ):
        db.insert_agent_owner_notification(_agent_owner_notification())


def test_insert_agent_owner_notification_rejects_thread_mismatch_with_session(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session(thread_bot_role="writer_agent"))
    with pytest.raises(
        ValueError,
        match="agent_owner_notification_thread_bot_role_mismatch",
    ):
        db.insert_agent_owner_notification(
            _agent_owner_notification(thread_bot_role="reviewer_agent")
        )


def test_list_queued_agent_owner_notifications_rejects_invalid_args(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        db.list_queued_agent_owner_notifications(
            0,
            "alpha_project",
            "writer_agent",
            "writer_agent",
        )
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.list_queued_agent_owner_notifications(
            101,
            "bad-id",
            "writer_agent",
            "writer_agent",
        )
    with pytest.raises(ValueError, match="invalid_agent_role"):
        db.list_queued_agent_owner_notifications(
            101,
            "alpha_project",
            "writer-agent",
            "writer_agent",
        )
    with pytest.raises(ValueError, match="invalid_thread_bot_role"):
        db.list_queued_agent_owner_notifications(
            101,
            "alpha_project",
            "writer_agent",
            "writer-agent",
        )


def test_mark_agent_owner_notification_delivered_rejects_invalid_args(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_notification_id"):
        db.mark_agent_owner_notification_delivered(0, delivered_at=1.0)
    with pytest.raises(ValueError, match="invalid_delivered_at"):
        db.mark_agent_owner_notification_delivered(1, delivered_at=0.0)


def test_mark_agent_owner_notification_delivered_rejects_repeated_delivery(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())
    inserted = db.insert_agent_owner_notification(_agent_owner_notification())

    db.mark_agent_owner_notification_delivered(
        inserted.notification_id,
        delivered_at=1004.0,
    )

    with pytest.raises(ValueError, match="notification_not_queued"):
        db.mark_agent_owner_notification_delivered(
            inserted.notification_id,
            delivered_at=1005.0,
        )


# ---------------------------------------------------------------------------
# Agent DM messages
# ---------------------------------------------------------------------------


def test_record_agent_dm_message_round_trip_returns_chronological_transcript(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())
    owner_message = _agent_dm_message(
        sender_kind="owner",
        sender_role="owner",
        body="Need a first draft",
        created_at=1001.0,
    )
    agent_message = _agent_dm_message(
        sender_kind="agent",
        sender_role="writer_agent",
        body="Draft is ready",
        created_at=1002.0,
    )

    db.record_agent_dm_message(owner_message)
    db.record_agent_dm_message(agent_message)

    assert db.list_agent_dm_messages(
        101,
        "alpha_project",
        "writer_agent",
    ) == (
        owner_message,
        agent_message,
    )


def test_list_agent_dm_messages_returns_empty_tuple_for_missing_transcript(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())

    assert db.list_agent_dm_messages(101, "alpha_project", "writer_agent") == ()


def test_record_agent_dm_message_enforces_default_retention_per_transcript(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())

    for index in range(DEFAULT_AGENT_DM_MESSAGE_MAXLEN + 5):
        db.record_agent_dm_message(
            _agent_dm_message(
                body=f"owner message {index}",
                created_at=1000.0 + index,
            )
        )

    messages = db.list_agent_dm_messages(101, "alpha_project", "writer_agent")

    assert len(messages) == DEFAULT_AGENT_DM_MESSAGE_MAXLEN
    assert messages[0].body == "owner message 5"
    assert messages[-1].body == "owner message 24"
    assert tuple(message.created_at for message in messages) == tuple(
        1000.0 + index
        for index in range(5, 25)
    )


def test_agent_dm_message_retention_does_not_affect_other_transcripts(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project(_project(project_id="beta_project", slug="beta-project"))
    db.upsert_agent_dm_session(_agent_dm_session())
    db.upsert_agent_dm_session(
        _agent_dm_session(
            project_id="beta_project",
            agent_role="reviewer_agent",
            thread_bot_role="reviewer_agent",
        )
    )

    for index in range(DEFAULT_AGENT_DM_MESSAGE_MAXLEN + 3):
        db.record_agent_dm_message(
            _agent_dm_message(
                body=f"writer message {index}",
                created_at=1000.0 + index,
            )
        )
    reviewer_message = _agent_dm_message(
        project_id="beta_project",
        agent_role="reviewer_agent",
        sender_kind="agent",
        sender_role="reviewer_agent",
        body="Review is ready",
        created_at=2000.0,
    )
    db.record_agent_dm_message(reviewer_message)

    writer_messages = db.list_agent_dm_messages(101, "alpha_project", "writer_agent")
    reviewer_messages = db.list_agent_dm_messages(
        101,
        "beta_project",
        "reviewer_agent",
    )

    assert len(writer_messages) == DEFAULT_AGENT_DM_MESSAGE_MAXLEN
    assert writer_messages[0].body == "writer message 3"
    assert reviewer_messages == (reviewer_message,)


def test_record_agent_dm_message_rejects_non_message_type(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_agent_dm_message_type:str"):
        db.record_agent_dm_message("bad")  # type: ignore[arg-type]


def test_record_agent_dm_message_rejects_missing_session(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    with pytest.raises(
        ValueError,
        match="missing_agent_dm_session:101:alpha_project:writer_agent",
    ):
        db.record_agent_dm_message(_agent_dm_message())


def test_record_agent_dm_message_rejects_closed_session(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session(status="closed"))

    with pytest.raises(
        ValueError,
        match="inactive_agent_dm_session:101:alpha_project:writer_agent:closed",
    ):
        db.record_agent_dm_message(_agent_dm_message())


def test_list_agent_dm_messages_rejects_invalid_args(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        db.list_agent_dm_messages(0, "alpha_project", "writer_agent")
    with pytest.raises(ValueError, match="invalid_project_id"):
        db.list_agent_dm_messages(101, "bad-id", "writer_agent")
    with pytest.raises(ValueError, match="invalid_agent_role"):
        db.list_agent_dm_messages(101, "alpha_project", "writer-agent")
    with pytest.raises(ValueError, match="invalid_limit"):
        db.list_agent_dm_messages(101, "alpha_project", "writer_agent", limit=0)


def test_trim_agent_dm_messages_supports_manual_windowing(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_agent_dm_session(_agent_dm_session())
    for index in range(5):
        db.record_agent_dm_message(
            _agent_dm_message(
                body=f"message {index}",
                created_at=1000.0 + index,
            ),
            max_entries=10,
        )

    db.trim_agent_dm_messages(101, "alpha_project", "writer_agent", 2)

    assert tuple(
        message.body
        for message in db.list_agent_dm_messages(101, "alpha_project", "writer_agent")
    ) == (
        "message 3",
        "message 4",
    )


# ---------------------------------------------------------------------------
# Migration v1 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v1_schema_with_schema_meta(tmp_path: Path):
    db_path = tmp_path / "v1.db"
    _build_v1_db(db_path, with_schema_meta=True)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    task = db.get_task("task-v1")
    assert task is not None
    assert task.branch == "feature/task-v1"
    assert db.get_budget(1) == pytest.approx(7.5)
    assert _table_names(db_path) >= {
        "projects",
        "project_policies",
        "project_members",
        "project_chat_bindings",
        "project_runtime_bindings",
    }


def test_migrates_v1_schema_without_schema_meta(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _build_v1_db(db_path, with_schema_meta=False)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(7.5)


def test_v1_task_history_migration_preserves_data(tmp_path: Path):
    db_path = tmp_path / "tasks.db"
    _build_v1_db(db_path, with_schema_meta=True)

    db = StateDB(db_path)
    task = db.get_task("task-v1")

    assert task is not None
    assert task.commit_sha == "deadbeef"
    assert task.final_state == "SUCCESS"
    assert task.tier_name == "STANDARD"


def test_v1_migration_renames_tier_name_column_to_active_tier(tmp_path: Path):
    db_path = tmp_path / "tiers.db"
    _build_v1_db(db_path, with_schema_meta=True)

    StateDB(db_path)

    assert "active_tier" in _table_columns(db_path, "tier_sessions")
    assert "tier_name" not in _table_columns(db_path, "tier_sessions")


def test_v1_migration_adds_autoincrement_id_to_task_history(tmp_path: Path):
    db_path = tmp_path / "history.db"
    _build_v1_db(db_path, with_schema_meta=True)

    StateDB(db_path)

    assert _table_columns(db_path, "task_history")[0] == "id"


# ---------------------------------------------------------------------------
# Migration v2 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v2_schema_to_v4_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v2.db"
    _build_v2_db(db_path)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    task = db.get_task("task-v2")
    assert task is not None
    assert task.branch == "feature/task-v2"
    assert task.commit_sha == "feedface"
    assert db.get_budget(1) == pytest.approx(8.25)


def test_v2_migration_adds_project_tables(tmp_path: Path):
    db_path = tmp_path / "v2-projects.db"
    _build_v2_db(db_path)

    StateDB(db_path)

    assert _table_names(db_path) >= {
        "projects",
        "project_policies",
        "project_members",
        "project_chat_bindings",
        "project_runtime_bindings",
    }


# ---------------------------------------------------------------------------
# Migration v3 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v3_schema_to_v4_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v3.db"
    _build_v3_db(db_path)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(9.5)
    task = db.get_task("task-v3")
    assert task is not None
    assert task.branch == "feature/task-v3"
    assert db.get_project("alpha_project") == _project()
    assert db.get_project_policy("alpha_project") == _policy()
    assert db.list_project_memberships("alpha_project") == [_membership()]
    assert db.get_project_chat_binding("alpha_project") == _binding()
    assert db.get_project_runtime_binding("alpha_project") is None


def test_v3_migration_adds_project_runtime_bindings_table(tmp_path: Path):
    db_path = tmp_path / "v3-runtime.db"
    _build_v3_db(db_path)

    StateDB(db_path)

    assert "project_runtime_bindings" in _table_names(db_path)


# ---------------------------------------------------------------------------
# Migration v4 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v4_schema_to_v5_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v4.db"
    _build_v4_db(db_path)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(10.5)
    task = db.get_task("task-v4")
    assert task is not None
    assert task.branch == "feature/task-v4"
    assert task.project_id is None
    assert db.get_project("alpha_project") == _project()


def test_v4_migration_adds_task_history_project_id_column(tmp_path: Path):
    db_path = tmp_path / "v4-history.db"
    _build_v4_db(db_path)

    StateDB(db_path)

    assert "project_id" in _table_columns(db_path, "task_history")


# ---------------------------------------------------------------------------
# Migration v5 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v5_schema_to_v6_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v5.db"
    repo = _git_repo(tmp_path, "v5-repo")
    _build_v5_db(db_path, repo)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(11.5)
    task = db.get_task("task-v5")
    assert task is not None
    assert task.branch == "feature/task-v5"
    assert task.project_id == "alpha_project"
    assert db.get_project("alpha_project") == _project()
    assert db.get_project_policy("alpha_project") == _policy()
    assert db.list_project_memberships("alpha_project") == [_membership()]
    assert db.get_project_chat_binding("alpha_project") == _binding()
    assert db.get_project_runtime_binding("alpha_project") == _runtime_binding(repo)


def test_v5_migration_adds_agent_dm_sessions_table(tmp_path: Path):
    db_path = tmp_path / "v5-dm.db"
    repo = _git_repo(tmp_path, "v5-dm-repo")
    _build_v5_db(db_path, repo)

    StateDB(db_path)

    assert "agent_dm_sessions" in _table_names(db_path)


# ---------------------------------------------------------------------------
# Migration v6 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v6_schema_to_v7_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v6.db"
    repo = _git_repo(tmp_path, "v6-repo")
    _build_v6_db(db_path, repo)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(11.5)
    task = db.get_task("task-v5")
    assert task is not None
    assert task.branch == "feature/task-v5"
    assert task.project_id == "alpha_project"
    assert db.get_project("alpha_project") == _project()
    assert db.get_project_policy("alpha_project") == _policy()
    assert db.list_project_memberships("alpha_project") == [_membership()]
    assert db.get_project_chat_binding("alpha_project") == _binding()
    assert db.get_project_runtime_binding("alpha_project") == _runtime_binding(repo)
    assert db.get_agent_dm_session(101, "alpha_project", "writer_agent") == (
        _agent_dm_session()
    )


def test_v6_migration_adds_agent_dm_messages_table(tmp_path: Path):
    db_path = tmp_path / "v6-messages.db"
    repo = _git_repo(tmp_path, "v6-messages-repo")
    _build_v6_db(db_path, repo)

    StateDB(db_path)

    assert "agent_dm_messages" in _table_names(db_path)


# ---------------------------------------------------------------------------
# Migration v7 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v7_schema_to_v9_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v7.db"
    repo = _git_repo(tmp_path, "v7-repo")
    _build_v7_db(db_path, repo)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(11.5)
    task = db.get_task("task-v5")
    assert task is not None
    assert task.branch == "feature/task-v5"
    assert task.project_id == "alpha_project"
    assert db.get_project("alpha_project") == _project()
    assert db.get_project_policy("alpha_project") == _policy()
    assert db.list_project_memberships("alpha_project") == [_membership()]
    assert db.get_project_chat_binding("alpha_project") == _binding()
    assert db.get_project_runtime_binding("alpha_project") == _runtime_binding(repo)
    assert db.get_agent_dm_session(101, "alpha_project", "writer_agent") == (
        _agent_dm_session()
    )
    assert db.list_agent_dm_messages(101, "alpha_project", "writer_agent") == (
        _agent_dm_message(body="Need a draft"),
    )
    assert db.list_queued_agent_owner_notifications(
        101,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    ) == ()


def test_v7_migration_adds_agent_owner_notifications_table(tmp_path: Path):
    db_path = tmp_path / "v7-notifications.db"
    repo = _git_repo(tmp_path, "v7-notifications-repo")
    _build_v7_db(db_path, repo)

    StateDB(db_path)

    assert "agent_owner_notifications" in _table_names(db_path)


# ---------------------------------------------------------------------------
# Migration v8 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v8_schema_to_v9_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v8.db"
    repo = _git_repo(tmp_path, "v8-repo")
    _build_v8_db(db_path, repo)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(11.5)
    task = db.get_task("task-v5")
    assert task is not None
    assert task.branch == "feature/task-v5"
    assert task.project_id == "alpha_project"
    assert db.get_project("alpha_project") == _project()
    assert db.get_project_policy("alpha_project") == _policy()
    assert db.list_project_memberships("alpha_project") == [_membership()]
    assert db.get_project_chat_binding("alpha_project") == _binding()
    assert db.get_project_runtime_binding("alpha_project") == _runtime_binding(repo)
    assert db.get_agent_dm_session(101, "alpha_project", "writer_agent") == (
        _agent_dm_session()
    )
    assert db.list_agent_dm_messages(101, "alpha_project", "writer_agent") == (
        _agent_dm_message(body="Need a draft"),
    )
    assert db.list_queued_agent_owner_notifications(
        101,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    ) == (_agent_owner_notification(notification_id=1),)


def test_v8_migration_adds_agent_bus_tables(tmp_path: Path):
    db_path = tmp_path / "v8-bus.db"
    repo = _git_repo(tmp_path, "v8-bus-repo")
    _build_v8_db(db_path, repo)

    StateDB(db_path)

    assert "project_threads" in _table_names(db_path)
    assert "agent_bus_messages" in _table_names(db_path)
    assert "project_specialist_roster" in _table_names(db_path)


# ---------------------------------------------------------------------------
# Migration v9 -> v10
# ---------------------------------------------------------------------------


def test_migrates_v9_schema_to_v10_and_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "v9.db"
    repo = _git_repo(tmp_path, "v9-repo")
    _build_v9_db(db_path, repo)

    db = StateDB(db_path)

    assert db.schema_version() == 11
    assert db.get_project("alpha_project") == _project()
    thread = db.get_project_thread("alpha_project", "thread_000001")
    assert thread is not None
    assert thread.task_id == "task-v9"
    message = db.get_agent_bus_message(
        "alpha_project",
        "thread_000001",
        "msg_000001",
    )
    assert message is not None
    assert message.body == "Need a first draft"
    assert db.get_project_specialist_roster("alpha_project") == (
        ProjectSpecialistRoster(
            project_id="alpha_project",
            specialist_roles=(),
        )
    )


def test_v9_migration_adds_project_specialist_roster_table(tmp_path: Path):
    db_path = tmp_path / "v9-roster.db"
    repo = _git_repo(tmp_path, "v9-roster-repo")
    _build_v9_db(db_path, repo)

    StateDB(db_path)

    assert "project_specialist_roster" in _table_names(db_path)
