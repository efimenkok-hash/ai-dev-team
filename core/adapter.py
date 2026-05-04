"""
core/adapter.py

Step 13 of the ULTRA spec: project adapters. An adapter is a frozen
description of a concrete external project — language, extra protected
paths, extra forbidden tokens, named commands (test/lint/build) with argv
tuples — and an AdapterRegistry that lets one orchestrator instance serve
multiple projects in parallel.

CONTRACTS:
1. All public dataclasses are frozen.
2. Adapter name matches r"^[a-z][a-z0-9_]{0,63}$" — snake_case ASCII.
3. project_path is validated at construction: it must exist and be a dir.
4. resolve_path(rel) refuses '..' and any path that escapes project_path.
5. ProjectCommand.cmd is a non-empty tuple[str, ...]. Each token is checked
   against shell metacharacters; if any token contains them, construction
   raises ValueError. This prevents shell-style injection at boot.
6. timeout_seconds > 0.
7. Forbidden paths and forbidden tokens are additive on top of
   core.contracts.FORBIDDEN_PATH_PARTS / forbidden_tokens — the adapter
   tightens the global policy, never relaxes it.
8. Serialization is stable: to_dict + from_dict round-trip via JSON.
9. AdapterRegistry rejects duplicate names; get() raises KeyError on miss.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_LANGUAGES = (
    "python",
    "typescript",
    "javascript",
    "go",
    "rust",
    "other",
)

VALID_SEVERITIES = ("error", "warning")

_ADAPTER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Disallowed substrings inside any command token. We refuse them eagerly
# rather than try to escape them — agents must not synthesise shell strings.
_SHELL_META_TOKENS = (
    ";",
    "|",
    "&",
    ">",
    "<",
    "`",
    "$(",
    "&&",
    "||",
    "\n",
    "\r",
)


@dataclass(frozen=True)
class ProjectRule:
    name: str
    description: str
    severity: str = "error"

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("empty_rule_name")
        if not self.description or not self.description.strip():
            raise ValueError("empty_rule_description")
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(f"unknown_severity:{self.severity}")


@dataclass(frozen=True)
class ProjectCommand:
    name: str
    cmd: tuple[str, ...]
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("empty_command_name")
        if not isinstance(self.cmd, tuple):
            raise ValueError("cmd_must_be_tuple")
        if not self.cmd:
            raise ValueError("empty_cmd")
        for token in self.cmd:
            if not isinstance(token, str):
                raise ValueError(f"non_string_cmd_token:{type(token).__name__}")
            if not token:
                raise ValueError("empty_cmd_token")
            for meta in _SHELL_META_TOKENS:
                if meta in token:
                    raise ValueError(f"shell_meta_in_cmd:{meta}")
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError(f"invalid_timeout_seconds:{self.timeout_seconds}")


def validate_adapter_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("empty_adapter_name")
    if not _ADAPTER_NAME_RE.match(name):
        raise ValueError(f"invalid_adapter_name:{name}")


@dataclass(frozen=True)
class ProjectAdapter:
    name: str
    project_path: Path
    language: str
    rules: tuple[ProjectRule, ...] = ()
    commands: Mapping[str, ProjectCommand] = field(default_factory=dict)
    forbidden_paths: tuple[str, ...] = ()
    forbidden_tokens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_adapter_name(self.name)

        if self.language not in VALID_LANGUAGES:
            raise ValueError(f"unknown_language:{self.language}")

        # Path resolution + existence check. We assign back the resolved Path
        # via object.__setattr__ because the dataclass is frozen.
        path = Path(self.project_path).resolve()
        if not path.exists():
            raise ValueError(f"project_path_missing:{path}")
        if not path.is_dir():
            raise ValueError(f"project_path_not_dir:{path}")
        object.__setattr__(self, "project_path", path)

        # Normalize commands to a frozen mapping.
        if not isinstance(self.commands, Mapping):
            raise ValueError("commands_must_be_mapping")
        normalized_cmds: dict[str, ProjectCommand] = {}
        for cmd_key, cmd_obj in self.commands.items():
            if not isinstance(cmd_key, str) or not cmd_key.strip():
                raise ValueError("empty_command_key")
            if not isinstance(cmd_obj, ProjectCommand):
                raise ValueError(f"invalid_command_value:{type(cmd_obj).__name__}")
            if cmd_key != cmd_obj.name:
                raise ValueError(
                    f"command_key_name_mismatch:{cmd_key}!={cmd_obj.name}"
                )
            normalized_cmds[cmd_key] = cmd_obj
        object.__setattr__(self, "commands", dict(normalized_cmds))

        # Validate forbidden_paths / forbidden_tokens shape.
        if not isinstance(self.forbidden_paths, tuple):
            raise ValueError("forbidden_paths_must_be_tuple")
        for fp in self.forbidden_paths:
            if not isinstance(fp, str) or not fp.strip():
                raise ValueError("empty_forbidden_path")
        if not isinstance(self.forbidden_tokens, tuple):
            raise ValueError("forbidden_tokens_must_be_tuple")
        for ft in self.forbidden_tokens:
            if not isinstance(ft, str) or not ft.strip():
                raise ValueError("empty_forbidden_token")

        if not isinstance(self.rules, tuple):
            raise ValueError("rules_must_be_tuple")

    def resolve_path(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel:
            raise ValueError("empty_rel_path")
        if ".." in Path(rel).parts:
            raise ValueError(f"path_escapes_project:{rel}")
        candidate = (self.project_path / rel).resolve()
        try:
            candidate.relative_to(self.project_path)
        except ValueError as exc:
            raise ValueError(f"path_outside_project:{rel}") from exc
        return candidate

    def get_command(self, name: str) -> ProjectCommand:
        if name not in self.commands:
            raise KeyError(f"unknown_command:{name}")
        return self.commands[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "project_path": str(self.project_path),
            "language": self.language,
            "rules": [
                {"name": r.name, "description": r.description, "severity": r.severity}
                for r in self.rules
            ],
            "commands": {
                k: {
                    "name": c.name,
                    "cmd": list(c.cmd),
                    "timeout_seconds": c.timeout_seconds,
                }
                for k, c in self.commands.items()
            },
            "forbidden_paths": list(self.forbidden_paths),
            "forbidden_tokens": list(self.forbidden_tokens),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectAdapter":
        if not isinstance(data, Mapping):
            raise ValueError("invalid_dump_type")
        if data.get("schema_version") != 1:
            raise ValueError(
                f"unsupported_schema_version:{data.get('schema_version')}"
            )
        for key in ("name", "project_path", "language"):
            if key not in data:
                raise ValueError(f"missing_dump_key:{key}")

        rules = tuple(
            ProjectRule(
                name=r["name"],
                description=r["description"],
                severity=r.get("severity", "error"),
            )
            for r in data.get("rules", ())
        )
        commands: dict[str, ProjectCommand] = {}
        for k, c in (data.get("commands") or {}).items():
            commands[k] = ProjectCommand(
                name=c["name"],
                cmd=tuple(c["cmd"]),
                timeout_seconds=int(c.get("timeout_seconds", 120)),
            )
        return cls(
            name=data["name"],
            project_path=Path(data["project_path"]),
            language=data["language"],
            rules=rules,
            commands=commands,
            forbidden_paths=tuple(data.get("forbidden_paths", ())),
            forbidden_tokens=tuple(data.get("forbidden_tokens", ())),
        )


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProjectAdapter] = {}

    def register(self, adapter: ProjectAdapter) -> None:
        if not isinstance(adapter, ProjectAdapter):
            raise ValueError(
                f"invalid_adapter_type:{type(adapter).__name__}"
            )
        if adapter.name in self._adapters:
            raise ValueError(f"adapter_already_registered:{adapter.name}")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ProjectAdapter:
        if name not in self._adapters:
            raise KeyError(f"unknown_adapter:{name}")
        return self._adapters[name]

    def list_names(self) -> list[str]:
        return sorted(self._adapters.keys())

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, name: object) -> bool:
        return name in self._adapters


def build_adapter_validators(
    adapter: ProjectAdapter,
) -> tuple:
    """Returns task validators derived from an adapter (for orchestrator).

    Currently this enforces forbidden_tokens against the *raw user task* —
    forbidden_paths apply to the patcher, not to the task text. Returns an
    empty tuple if there are no relevant policies.
    """
    if not adapter.forbidden_tokens:
        return ()

    bad_tokens = tuple(adapter.forbidden_tokens)

    def _check(raw: str) -> None:
        if not isinstance(raw, str):
            raise ValueError("non_string_task")
        for tok in bad_tokens:
            if tok in raw:
                raise ValueError(f"adapter_forbidden_token:{tok}")

    return (_check,)


def load_adapters_from_iterable(
    items: Iterable[Mapping[str, Any]],
) -> AdapterRegistry:
    """Convenience: build an AdapterRegistry from a list of dump_dict's."""
    registry = AdapterRegistry()
    for raw in items:
        registry.register(ProjectAdapter.from_dict(raw))
    return registry
