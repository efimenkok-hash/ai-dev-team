from __future__ import annotations

from dataclasses import dataclass

from core.agent_role_catalog import is_specialist_role
from core.bot_commands import BotCommand, CommandName
from core.hire_approval import (
    HireApprovalDecision,
    HireApprovalService,
    PendingHireRequest,
)
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster

_ALLOWED_ACTIONS = frozenset(
    {"list", "add", "remove", "pending", "approve", "reject"}
)
_ALLOWED_CONTEXT_SOURCES = frozenset({"bound_chat", "owner_dm_single_project"})


def _normalize_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("empty_project_id")
    normalized = project_id.strip().lower()
    if not normalized.isascii():
        raise ValueError("non_ascii_project_id")
    if not normalized[0].isalpha():
        raise ValueError(f"invalid_project_id:{normalized}")
    for char in normalized:
        if not (char.islower() or char.isdigit() or char == "_"):
            raise ValueError(f"invalid_project_id:{normalized}")
    if len(normalized) > 64:
        raise ValueError(f"invalid_project_id:{normalized}")
    return normalized


def _normalize_specialist_role(role: str) -> str:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("empty_specialist_role")
    normalized = role.strip().lower()
    if not is_specialist_role(normalized):
        raise ValueError(f"unknown_specialist_role:{normalized}")
    return normalized


def _normalize_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("empty_hire_request_id")
    normalized = request_id.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"invalid_hire_request_id:{normalized}")
    for char in normalized:
        if not (char.islower() or char.isdigit() or char in {"-", "_"}):
            raise ValueError(f"invalid_hire_request_id:{normalized}")
    return normalized


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def format_project_team_usage() -> str:
    return (
        "Использование:\n"
        "  /team\n"
        "  /team list\n"
        "  /team pending\n"
        "  /team add <security_agent|devops_agent|data_agent>\n"
        "  /team remove <security_agent|devops_agent|data_agent>\n"
        "  /team approve <request_id>\n"
        "  /team reject <request_id>"
    )


def describe_project_team_command_error(code: str) -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValueError("empty_project_team_error_code")
    normalized = code.strip()
    if normalized == "project_team_command_requires_team":
        return "Эта команда обрабатывает только `/team`."
    if normalized in {
        "project_team_invalid_subcommand",
        "project_team_extra_args",
        "project_team_list_forbids_role",
        "project_team_action_requires_role",
        "project_team_action_requires_request_id",
        "project_team_request_action_forbids_role",
        "empty_hire_request_id",
    }:
        return (
            "Неверный синтаксис `/team`.\n"
            "\n"
            f"{format_project_team_usage()}"
        )
    if normalized.startswith("invalid_hire_request_id:"):
        return (
            "Указан некорректный request id.\n"
            "\n"
            f"{format_project_team_usage()}"
        )
    if normalized.startswith("unknown_specialist_role:"):
        return (
            "Указана неизвестная specialist role.\n"
            "\n"
            f"{format_project_team_usage()}"
        )
    if normalized == "project_team_mutation_requires_owner":
        return "Изменять specialist roster и pending hire requests может только owner проекта."
    if normalized == "project_team_mutation_disallowed_by_policy":
        return (
            "Для этого проекта изменение specialist roster сейчас запрещено "
            "политикой (`allow_hiring = false`)."
        )
    if normalized == "project_team_policy_unavailable":
        return (
            "Project policy для этого проекта сейчас недоступна, поэтому "
            "изменить specialist roster нельзя."
        )
    if normalized.startswith("duplicate_project_specialist:"):
        _, project_id, specialist_role = normalized.split(":", 2)
        return (
            f"`{specialist_role}` уже присутствует в persisted roster проекта "
            f"`{project_id}`."
        )
    if normalized.startswith("unknown_project_specialist:"):
        _, project_id, specialist_role = normalized.split(":", 2)
        return (
            f"`{specialist_role}` сейчас отсутствует в persisted roster проекта "
            f"`{project_id}`."
        )
    if normalized == "hire_approval_requires_owner":
        return "Approve/reject pending hire requests может только owner проекта."
    if normalized.startswith("unknown_project_id:"):
        _, project_id = normalized.split(":", 1)
        return f"Проект `{project_id}` не найден."
    return f"Операция `/team` не выполнена. Техническая причина: `{normalized}`"


@dataclass(frozen=True)
class ProjectTeamCommand:
    action: str
    specialist_role: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or self.action not in _ALLOWED_ACTIONS:
            raise ValueError(f"invalid_project_team_action:{self.action!r}")
        if self.action in {"list", "pending"}:
            if self.specialist_role is not None or self.request_id is not None:
                raise ValueError("project_team_list_forbids_role")
            return
        if self.action in {"add", "remove"}:
            if self.request_id is not None:
                raise ValueError("project_team_request_action_forbids_role")
            if self.specialist_role is None:
                raise ValueError("project_team_action_requires_role")
            object.__setattr__(
                self,
                "specialist_role",
                _normalize_specialist_role(self.specialist_role),
            )
            return
        if self.specialist_role is not None:
            raise ValueError("project_team_request_action_forbids_role")
        if self.request_id is None:
            raise ValueError("project_team_action_requires_request_id")
        object.__setattr__(
            self,
            "request_id",
            _normalize_request_id(self.request_id),
        )


@dataclass(frozen=True)
class ProjectTeamCommandContext:
    snapshot: ProjectSnapshot
    actor_user_id: int
    context_source: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        object.__setattr__(
            self,
            "actor_user_id",
            _validate_positive_int(self.actor_user_id, field_name="actor_user_id"),
        )
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in _ALLOWED_CONTEXT_SOURCES
        ):
            raise ValueError(
                "invalid_project_team_context_source:"
                f"{self.context_source!r}"
            )


@dataclass(frozen=True)
class ProjectTeamCommandResult:
    project_id: str
    action: str
    roster: ProjectSpecialistRoster
    message_text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_project_id(self.project_id),
        )
        if not isinstance(self.action, str) or self.action not in _ALLOWED_ACTIONS:
            raise ValueError(f"invalid_project_team_action:{self.action!r}")
        if not isinstance(self.roster, ProjectSpecialistRoster):
            raise ValueError(
                "invalid_project_specialist_roster_type:"
                f"{type(self.roster).__name__}"
            )
        if self.roster.project_id != self.project_id:
            raise ValueError(
                "project_team_result_project_id_mismatch:"
                f"{self.roster.project_id}!={self.project_id}"
            )
        if not isinstance(self.message_text, str) or not self.message_text.strip():
            raise ValueError("empty_project_team_message_text")


def parse_project_team_command(command: BotCommand) -> ProjectTeamCommand:
    if not isinstance(command, BotCommand):
        raise ValueError(
            f"invalid_project_team_bot_command_type:{type(command).__name__}"
        )
    if command.name is not CommandName.TEAM:
        raise ValueError("project_team_command_requires_team")
    positional = command.positional_args()
    if not positional:
        return ProjectTeamCommand(action="list")
    action = positional[0].strip().lower()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("project_team_invalid_subcommand")
    if action in {"list", "pending"}:
        if len(positional) != 1:
            raise ValueError("project_team_extra_args")
        return ProjectTeamCommand(action=action)
    if action in {"add", "remove"}:
        if len(positional) != 2:
            raise ValueError("project_team_extra_args")
        return ProjectTeamCommand(
            action=action,
            specialist_role=positional[1],
        )
    if len(positional) != 2:
        raise ValueError("project_team_extra_args")
    return ProjectTeamCommand(
        action=action,
        request_id=positional[1],
    )


class ProjectTeamCommandService:
    def __init__(self, registry: ProjectRegistry) -> None:
        if not isinstance(registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(registry).__name__}"
            )
        self._registry = registry
        self._hire_approval_service = HireApprovalService(registry)

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def hire_approval_service(self) -> HireApprovalService:
        return self._hire_approval_service

    def handle(
        self,
        command: ProjectTeamCommand,
        context: ProjectTeamCommandContext,
    ) -> ProjectTeamCommandResult:
        if not isinstance(command, ProjectTeamCommand):
            raise ValueError(
                "invalid_project_team_command_type:"
                f"{type(command).__name__}"
            )
        if not isinstance(context, ProjectTeamCommandContext):
            raise ValueError(
                "invalid_project_team_command_context_type:"
                f"{type(context).__name__}"
            )
        project = context.snapshot.project
        if command.action == "list":
            roster = self._registry.get_project_specialist_roster(project.project_id)
            return ProjectTeamCommandResult(
                project_id=project.project_id,
                action="list",
                roster=roster,
                message_text=self._render_message(
                    context,
                    roster,
                    status_line=None,
                ),
            )
        if command.action == "pending":
            roster = self._registry.get_project_specialist_roster(project.project_id)
            pending_requests = self._hire_approval_service.list_pending_requests(
                project.project_id
            )
            return ProjectTeamCommandResult(
                project_id=project.project_id,
                action="pending",
                roster=roster,
                message_text=self._render_message(
                    context,
                    roster,
                    status_line=None,
                    pending_requests=pending_requests,
                ),
            )

        if command.action in {"add", "remove"}:
            self._validate_direct_mutation_allowed(context)
            assert command.specialist_role is not None
            if command.action == "add":
                roster = self._registry.add_project_specialist(
                    project.project_id,
                    command.specialist_role,
                )
                reconciled_requests = self._reconcile_matching_pending_requests(
                    context,
                    command.specialist_role,
                )
                status_line = (
                    "Status: "
                    f"`{command.specialist_role}` добавлен в persisted specialist "
                    "roster проекта."
                )
                if reconciled_requests:
                    request_ids = ", ".join(
                        f"`{request.request_id}`"
                        for request in reconciled_requests
                    )
                    status_line += (
                        " Matching pending hire requests marked approved as "
                        f"direct owner approval: {request_ids}."
                    )
            else:
                roster = self._registry.remove_project_specialist(
                    project.project_id,
                    command.specialist_role,
                )
                status_line = (
                    "Status: "
                    f"`{command.specialist_role}` удалён из persisted specialist "
                    "roster проекта."
                )
            return ProjectTeamCommandResult(
                project_id=project.project_id,
                action=command.action,
                roster=roster,
                message_text=self._render_message(
                    context,
                    roster,
                    status_line=status_line,
                ),
            )

        self._validate_owner(context)
        assert command.request_id is not None
        approval_result = self._hire_approval_service.apply_decision(
            context.snapshot,
            HireApprovalDecision(
                request_id=command.request_id,
                decision=command.action,
                actor_user_id=context.actor_user_id,
            ),
        )
        return ProjectTeamCommandResult(
            project_id=project.project_id,
            action=command.action,
            roster=approval_result.roster_after,
            message_text=self._render_message(
                context,
                approval_result.roster_after,
                status_line=approval_result.message_text,
            ),
        )

    def _reconcile_matching_pending_requests(
        self,
        context: ProjectTeamCommandContext,
        specialist_role: str,
    ) -> tuple[PendingHireRequest, ...]:
        project_id = context.snapshot.project.project_id
        pending_requests = self._hire_approval_service.list_pending_requests(
            project_id
        )
        reconciled: list[PendingHireRequest] = []
        for request in pending_requests:
            if request.specialist_role != specialist_role:
                continue
            reconciled.append(
                self._registry.approve_hire_request(
                    request.request_id,
                    context.actor_user_id,
                )
            )
        return tuple(reconciled)

    def _validate_owner(
        self,
        context: ProjectTeamCommandContext,
    ) -> None:
        snapshot = context.snapshot
        if context.actor_user_id != snapshot.project.owner_user_id:
            raise ValueError("project_team_mutation_requires_owner")

    def _validate_direct_mutation_allowed(
        self,
        context: ProjectTeamCommandContext,
    ) -> None:
        project_id = context.snapshot.project.project_id
        current_snapshot = self._registry.get_project_snapshot(project_id)
        if current_snapshot is None:
            raise ValueError(f"unknown_project_id:{project_id}")
        if context.actor_user_id != current_snapshot.project.owner_user_id:
            raise ValueError("project_team_mutation_requires_owner")
        if current_snapshot.policy is None:
            raise ValueError("project_team_policy_unavailable")
        if not current_snapshot.policy.allow_hiring:
            raise ValueError("project_team_mutation_disallowed_by_policy")

    @staticmethod
    def _render_message(
        context: ProjectTeamCommandContext,
        roster: ProjectSpecialistRoster,
        *,
        status_line: str | None,
        pending_requests: tuple[PendingHireRequest, ...] | None = None,
    ) -> str:
        project = context.snapshot.project
        lines = [
            "👥 Project team",
            "",
            f"project: `{project.slug}` (`{project.project_id}`)",
            "",
            "Project specialists:",
        ]
        if roster.is_empty:
            lines.append("- none")
        else:
            for specialist_role in roster.specialist_roles:
                lines.append(f"- {specialist_role}")
        if pending_requests is not None:
            lines.extend(["", "Pending hire requests:"])
            if not pending_requests:
                lines.append("- none")
            else:
                for request in pending_requests:
                    lines.append(
                        f"- `{request.request_id}` · {request.specialist_role} · {request.reason}"
                    )
        if status_line is not None:
            lines.extend(["", status_line])
        return "\n".join(lines)
