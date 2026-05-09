"""
core/project_runtime.py

Runtime binding template for project-aware execution.

Scope for roadmap step P1.4:
1. Persist the per-project runtime template independently from live runtime
   objects.
2. Validate repo / worktree / adapter-facing config eagerly.
3. Materialize SandboxConfig and ProjectAdapter on demand without wiring them
   into bot_runner or task execution yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.adapter import (
    VALID_LANGUAGES,
    ProjectAdapter,
    ProjectCommand,
    ProjectRule,
    validate_adapter_name,
)
from core.sandbox_workspace import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_BRANCH_PREFIX,
    SandboxConfig,
)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHELL_META_TOKENS = (";", "|", "&", ">", "<", "`", "$(", "&&", "||", "\n", "\r")


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_branch_name(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip()
    if any(meta in normalized for meta in _SHELL_META_TOKENS):
        raise ValueError(f"shell_meta_in_{field_name}:{normalized!r}")
    if not _BRANCH_NAME_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized!r}")
    return normalized


def _normalize_branch_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty_branch_prefix")
    normalized = value.strip()
    if any(meta in normalized for meta in _SHELL_META_TOKENS):
        raise ValueError(f"shell_meta_in_branch_prefix:{normalized!r}")
    if not _BRANCH_NAME_RE.fullmatch(f"{normalized}task-42"):
        raise ValueError(f"invalid_branch_prefix:{normalized!r}")
    return normalized


def _normalize_text_tuple(
    value: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name}_must_be_tuple")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"empty_{field_name[:-1]}")
        normalized.append(item.strip())
    return tuple(normalized)


@dataclass(frozen=True)
class ProjectRuntimeBinding:
    project_id: str
    adapter_name: str
    repo_path: Path
    worktree_root: Path | None = None
    base_branch: str = DEFAULT_BASE_BRANCH
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    language: str = "python"
    rules: tuple[ProjectRule, ...] = ()
    commands: tuple[ProjectCommand, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    forbidden_tokens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )

        if not isinstance(self.adapter_name, str) or not self.adapter_name.strip():
            raise ValueError("empty_adapter_name")
        normalized_adapter_name = self.adapter_name.strip()
        validate_adapter_name(normalized_adapter_name)
        object.__setattr__(self, "adapter_name", normalized_adapter_name)

        if not isinstance(self.repo_path, Path):
            raise ValueError("repo_path_must_be_path")
        resolved_repo = self.repo_path.resolve()
        if not resolved_repo.exists():
            raise ValueError(f"repo_path_missing:{resolved_repo}")
        if not resolved_repo.is_dir():
            raise ValueError(f"repo_path_not_dir:{resolved_repo}")
        if not (resolved_repo / ".git").exists():
            raise ValueError(f"repo_path_not_git:{resolved_repo}")
        object.__setattr__(self, "repo_path", resolved_repo)

        resolved_worktree_root: Path | None = None
        if self.worktree_root is not None:
            if not isinstance(self.worktree_root, Path):
                raise ValueError("worktree_root_must_be_path_or_none")
            resolved_worktree_root = self.worktree_root.resolve()
            if resolved_worktree_root.exists() and not resolved_worktree_root.is_dir():
                raise ValueError(f"worktree_root_not_dir:{resolved_worktree_root}")
            try:
                resolved_worktree_root.relative_to(resolved_repo)
                raise ValueError("worktree_root_inside_repo_path")
            except ValueError as exc:
                if str(exc) == "worktree_root_inside_repo_path":
                    raise
            object.__setattr__(self, "worktree_root", resolved_worktree_root)

        object.__setattr__(
            self,
            "base_branch",
            _normalize_branch_name(self.base_branch, field_name="base_branch"),
        )
        object.__setattr__(
            self,
            "branch_prefix",
            _normalize_branch_prefix(self.branch_prefix),
        )

        if self.language not in VALID_LANGUAGES:
            raise ValueError(f"unknown_language:{self.language}")

        if not isinstance(self.rules, tuple):
            raise ValueError("rules_must_be_tuple")
        normalized_rules: list[ProjectRule] = []
        for rule in self.rules:
            if not isinstance(rule, ProjectRule):
                raise ValueError(f"invalid_rule_type:{type(rule).__name__}")
            normalized_rules.append(rule)
        object.__setattr__(self, "rules", tuple(normalized_rules))

        if not isinstance(self.commands, tuple):
            raise ValueError("commands_must_be_tuple")
        normalized_commands: list[ProjectCommand] = []
        command_names: set[str] = set()
        for command in self.commands:
            if not isinstance(command, ProjectCommand):
                raise ValueError(f"invalid_command_type:{type(command).__name__}")
            if command.name in command_names:
                raise ValueError(f"duplicate_command_name:{command.name}")
            command_names.add(command.name)
            normalized_commands.append(command)
        object.__setattr__(self, "commands", tuple(normalized_commands))

        object.__setattr__(
            self,
            "forbidden_paths",
            _normalize_text_tuple(
                self.forbidden_paths,
                field_name="forbidden_paths",
            ),
        )
        object.__setattr__(
            self,
            "forbidden_tokens",
            _normalize_text_tuple(
                self.forbidden_tokens,
                field_name="forbidden_tokens",
            ),
        )

        if resolved_worktree_root is not None:
            sandbox_kwargs = {
                "main_repo_path": resolved_repo,
                "worktree_root": resolved_worktree_root,
                "base_branch": self.base_branch,
                "branch_prefix": self.branch_prefix,
            }
        else:
            sandbox_kwargs = {
                "main_repo_path": resolved_repo,
                "base_branch": self.base_branch,
                "branch_prefix": self.branch_prefix,
            }
        SandboxConfig(**sandbox_kwargs)

    def build_sandbox_config(self) -> SandboxConfig:
        kwargs: dict[str, object] = {
            "main_repo_path": self.repo_path,
            "base_branch": self.base_branch,
            "branch_prefix": self.branch_prefix,
        }
        if self.worktree_root is not None:
            kwargs["worktree_root"] = self.worktree_root
        return SandboxConfig(**kwargs)

    def build_adapter(self, project_path: Path) -> ProjectAdapter:
        if not isinstance(project_path, Path):
            raise ValueError("project_path_must_be_path")
        return ProjectAdapter(
            name=self.adapter_name,
            project_path=project_path,
            language=self.language,
            rules=self.rules,
            commands={command.name: command for command in self.commands},
            forbidden_paths=self.forbidden_paths,
            forbidden_tokens=self.forbidden_tokens,
        )
