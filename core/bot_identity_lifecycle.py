from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_runtime import (
    VALID_MULTI_BOT_RUNTIME_SOURCES,
    BotIdentity,
    MultiBotRuntimeSpec,
)

_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _normalize_role(role: str) -> str:
    if not isinstance(role, str):
        raise ValueError(f"invalid_lifecycle_role_type:{type(role).__name__}")
    normalized = role.strip().lower()
    if not normalized:
        raise ValueError("empty_lifecycle_role")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_lifecycle_role:{normalized}")
    if not _ROLE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_lifecycle_role:{normalized}")
    return normalized


def _validate_failure_reason(reason: str | None, *, field_name: str) -> None:
    if reason is None:
        return
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"invalid_{field_name}")


def _validate_bot_username(username: str | None) -> None:
    if username is None:
        return
    if not isinstance(username, str) or not username.strip():
        raise ValueError("invalid_bot_username")
    normalized = username.strip()
    if normalized.startswith("@"):
        raise ValueError(f"invalid_bot_username:{normalized}")
    if not _BOT_USERNAME_RE.fullmatch(normalized):
        raise ValueError(f"invalid_bot_username:{normalized}")


@dataclass(frozen=True)
class BotIdentityReachability:
    identity: BotIdentity
    bot_user_id: int | None
    bot_username: str | None
    token_valid: bool
    reachable: bool
    failure_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BotIdentity):
            raise ValueError(
                "invalid_bot_identity_type:"
                f"{type(self.identity).__name__}"
            )
        if not isinstance(self.token_valid, bool):
            raise ValueError("invalid_token_valid_flag")
        if not isinstance(self.reachable, bool):
            raise ValueError("invalid_reachable_flag")
        if self.bot_user_id is not None and (
            not isinstance(self.bot_user_id, int)
            or isinstance(self.bot_user_id, bool)
            or self.bot_user_id <= 0
        ):
            raise ValueError(f"invalid_bot_user_id:{self.bot_user_id!r}")
        _validate_bot_username(self.bot_username)
        _validate_failure_reason(
            self.failure_reason,
            field_name="reachability_failure_reason",
        )
        if self.reachable:
            if not self.token_valid:
                raise ValueError("reachable_requires_valid_token")
            if self.bot_user_id is None:
                raise ValueError("reachable_requires_bot_user_id")
            if self.bot_username is None:
                raise ValueError("reachable_requires_bot_username")


@dataclass(frozen=True)
class BotIdentityLifecycleState:
    identity: BotIdentity
    reachability: BotIdentityReachability
    application_built: bool
    initialized: bool
    started: bool
    polling_started: bool
    failure_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BotIdentity):
            raise ValueError(
                "invalid_bot_identity_type:"
                f"{type(self.identity).__name__}"
            )
        if not isinstance(self.reachability, BotIdentityReachability):
            raise ValueError(
                "invalid_bot_identity_reachability_type:"
                f"{type(self.reachability).__name__}"
            )
        for field_name, value in (
            ("application_built", self.application_built),
            ("initialized", self.initialized),
            ("started", self.started),
            ("polling_started", self.polling_started),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"invalid_{field_name}_flag")
        _validate_failure_reason(
            self.failure_reason,
            field_name="lifecycle_failure_reason",
        )
        if self.reachability.identity.agent_role != self.identity.agent_role:
            raise ValueError(
                "lifecycle_identity_reachability_role_mismatch:"
                f"{self.identity.agent_role}!={self.reachability.identity.agent_role}"
            )
        if self.initialized and not self.application_built:
            raise ValueError("initialized_requires_application_built")
        if self.started and not self.application_built:
            raise ValueError("started_requires_application_built")
        if self.polling_started and not self.started:
            raise ValueError("polling_started_requires_started")
        if self.polling_started and not self.initialized:
            raise ValueError("polling_started_requires_initialized")


@dataclass(frozen=True)
class MultiBotLifecycleReport:
    source: str
    primary_role: str
    states_by_role: Mapping[str, BotIdentityLifecycleState]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, str)
            or self.source not in VALID_MULTI_BOT_RUNTIME_SOURCES
        ):
            raise ValueError(f"invalid_multi_bot_lifecycle_source:{self.source!r}")
        normalized_primary_role = _normalize_role(self.primary_role)
        if normalized_primary_role != COORDINATOR_ROLE:
            raise ValueError(
                "lifecycle_primary_role_must_be_coordinator_agent:"
                f"{normalized_primary_role}"
            )
        if not isinstance(self.states_by_role, Mapping):
            raise ValueError(
                "invalid_lifecycle_states_by_role_type:"
                f"{type(self.states_by_role).__name__}"
            )
        normalized: dict[str, BotIdentityLifecycleState] = {}
        for role in sorted(self.states_by_role.keys()):
            normalized_role = _normalize_role(role)
            state = self.states_by_role[role]
            if not isinstance(state, BotIdentityLifecycleState):
                raise ValueError(
                    "invalid_bot_identity_lifecycle_state_type:"
                    f"{type(state).__name__}"
                )
            if state.identity.agent_role != normalized_role:
                raise ValueError(
                    "lifecycle_state_role_mismatch:"
                    f"{normalized_role}!={state.identity.agent_role}"
                )
            normalized[normalized_role] = state
        if not normalized:
            raise ValueError("empty_lifecycle_states")
        if COORDINATOR_ROLE not in normalized:
            raise ValueError("missing_lifecycle_primary_role:coordinator_agent")
        if self.source == "single_token_legacy" and tuple(normalized.keys()) != (
            COORDINATOR_ROLE,
        ):
            raise ValueError("single_token_legacy_requires_coordinator_only_report")
        object.__setattr__(self, "primary_role", normalized_primary_role)
        object.__setattr__(
            self,
            "states_by_role",
            MappingProxyType(normalized),
        )


class BotIdentityLifecycleService:
    def build_initial_report(
        self,
        runtime_spec: MultiBotRuntimeSpec,
    ) -> MultiBotLifecycleReport:
        if not isinstance(runtime_spec, MultiBotRuntimeSpec):
            raise ValueError(
                "invalid_multi_bot_runtime_spec_type:"
                f"{type(runtime_spec).__name__}"
            )
        states_by_role = {
            role: BotIdentityLifecycleState(
                identity=identity,
                reachability=BotIdentityReachability(
                    identity=identity,
                    bot_user_id=None,
                    bot_username=None,
                    token_valid=False,
                    reachable=False,
                    failure_reason=None,
                ),
                application_built=True,
                initialized=False,
                started=False,
                polling_started=False,
                failure_reason=None,
            )
            for role, identity in runtime_spec.role_map.by_role.items()
        }
        return MultiBotLifecycleReport(
            source=runtime_spec.source,
            primary_role=runtime_spec.primary_bot.agent_role,
            states_by_role=states_by_role,
        )

    def mark_reachable(
        self,
        report: MultiBotLifecycleReport,
        role: str,
        *,
        bot_user_id: int,
        bot_username: str,
    ) -> MultiBotLifecycleReport:
        state = self._resolve_state(report, role)
        updated_reachability = BotIdentityReachability(
            identity=state.identity,
            bot_user_id=bot_user_id,
            bot_username=bot_username,
            token_valid=True,
            reachable=True,
            failure_reason=None,
        )
        return self._replace_state(
            report,
            role,
            replace(
                state,
                reachability=updated_reachability,
                failure_reason=None,
            ),
        )

    def mark_initialized(
        self,
        report: MultiBotLifecycleReport,
        role: str,
    ) -> MultiBotLifecycleReport:
        state = self._resolve_state(report, role)
        return self._replace_state(
            report,
            role,
            replace(
                state,
                application_built=True,
                initialized=True,
                failure_reason=None,
            ),
        )

    def mark_started(
        self,
        report: MultiBotLifecycleReport,
        role: str,
    ) -> MultiBotLifecycleReport:
        state = self._resolve_state(report, role)
        return self._replace_state(
            report,
            role,
            replace(
                state,
                application_built=True,
                initialized=True,
                started=True,
                failure_reason=None,
            ),
        )

    def mark_polling_started(
        self,
        report: MultiBotLifecycleReport,
        role: str,
    ) -> MultiBotLifecycleReport:
        state = self._resolve_state(report, role)
        return self._replace_state(
            report,
            role,
            replace(
                state,
                application_built=True,
                initialized=True,
                started=True,
                polling_started=True,
                failure_reason=None,
            ),
        )

    def mark_failure(
        self,
        report: MultiBotLifecycleReport,
        role: str,
        reason: str,
    ) -> MultiBotLifecycleReport:
        state = self._resolve_state(report, role)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("invalid_lifecycle_failure_reason")
        updated_reachability = state.reachability
        if not state.reachability.reachable:
            updated_reachability = replace(
                updated_reachability,
                failure_reason=reason.strip(),
            )
        return self._replace_state(
            report,
            role,
            replace(
                state,
                reachability=updated_reachability,
                failure_reason=reason.strip(),
            ),
        )

    def format_report(
        self,
        report: MultiBotLifecycleReport,
    ) -> str:
        if not isinstance(report, MultiBotLifecycleReport):
            raise ValueError(
                "invalid_multi_bot_lifecycle_report_type:"
                f"{type(report).__name__}"
            )
        lines = [
            "Bot lifecycle report",
            f"source={report.source}",
            f"primary_role={report.primary_role}",
        ]
        for role, state in report.states_by_role.items():
            username = (
                f"@{state.reachability.bot_username}"
                if state.reachability.bot_username is not None
                else "?"
            )
            bot_user_id = (
                str(state.reachability.bot_user_id)
                if state.reachability.bot_user_id is not None
                else "?"
            )
            lines.append(
                f"- {role}: token_valid={state.reachability.token_valid} "
                f"reachable={state.reachability.reachable} "
                f"bot={username} id={bot_user_id} "
                f"built={state.application_built} "
                f"initialized={state.initialized} "
                f"started={state.started} "
                f"polling_started={state.polling_started} "
                f"failure={state.failure_reason or state.reachability.failure_reason or '-'}"
            )
        return "\n".join(lines)

    def _resolve_state(
        self,
        report: MultiBotLifecycleReport,
        role: str,
    ) -> BotIdentityLifecycleState:
        if not isinstance(report, MultiBotLifecycleReport):
            raise ValueError(
                "invalid_multi_bot_lifecycle_report_type:"
                f"{type(report).__name__}"
            )
        normalized_role = _normalize_role(role)
        state = report.states_by_role.get(normalized_role)
        if state is None:
            raise ValueError(f"unknown_lifecycle_role:{normalized_role}")
        return state

    def _replace_state(
        self,
        report: MultiBotLifecycleReport,
        role: str,
        state: BotIdentityLifecycleState,
    ) -> MultiBotLifecycleReport:
        normalized_role = _normalize_role(role)
        updated = dict(report.states_by_role)
        updated[normalized_role] = state
        return MultiBotLifecycleReport(
            source=report.source,
            primary_role=report.primary_role,
            states_by_role=updated,
        )
