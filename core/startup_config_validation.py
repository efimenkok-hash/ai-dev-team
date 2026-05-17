from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from core.env_layout import (
    BotRuntimeEnvConfig,
    SharedRuntimeEnvConfig,
    WebRuntimeEnvConfig,
)
from core.multi_bot_runtime import build_multi_bot_runtime_spec

VALID_STARTUP_VALIDATION_SCOPES = frozenset({"shared", "bot", "web"})
VALID_STARTUP_VALIDATION_SEVERITIES = frozenset({"error", "warning"})
VALID_LOG_LEVEL_NAMES = frozenset(logging.getLevelNamesMapping().keys())


@dataclass(frozen=True)
class StartupValidationIssue:
    scope: str
    severity: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, str)
            or self.scope not in VALID_STARTUP_VALIDATION_SCOPES
        ):
            raise ValueError(f"invalid_startup_validation_scope:{self.scope!r}")
        if (
            not isinstance(self.severity, str)
            or self.severity not in VALID_STARTUP_VALIDATION_SEVERITIES
        ):
            raise ValueError(
                f"invalid_startup_validation_severity:{self.severity!r}"
            )
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("empty_startup_validation_code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("empty_startup_validation_message")
        object.__setattr__(self, "code", self.code.strip())
        object.__setattr__(self, "message", self.message.strip())


@dataclass(frozen=True)
class StartupValidationReport:
    scope: str
    issues: tuple[StartupValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, str)
            or self.scope not in VALID_STARTUP_VALIDATION_SCOPES
        ):
            raise ValueError(f"invalid_startup_validation_scope:{self.scope!r}")
        if not isinstance(self.issues, tuple):
            raise ValueError("startup_validation_issues_must_be_tuple")
        for issue in self.issues:
            if not isinstance(issue, StartupValidationIssue):
                raise ValueError(
                    "invalid_startup_validation_issue_type:"
                    f"{type(issue).__name__}"
                )

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(issue.severity == "warning" for issue in self.issues)

    @property
    def is_valid(self) -> bool:
        return not self.has_errors


class StartupConfigValidationError(RuntimeError):
    def __init__(self, report: StartupValidationReport) -> None:
        if not isinstance(report, StartupValidationReport):
            raise ValueError(
                "invalid_startup_validation_report_type:"
                f"{type(report).__name__}"
            )
        self.report = report
        super().__init__(format_startup_validation_report(report))


def _issue(
    *,
    scope: str,
    severity: str,
    code: str,
    message: str,
) -> StartupValidationIssue:
    return StartupValidationIssue(
        scope=scope,
        severity=severity,
        code=code,
        message=message,
    )


def format_startup_validation_report(
    report: StartupValidationReport,
) -> str:
    if not isinstance(report, StartupValidationReport):
        raise ValueError(
            "invalid_startup_validation_report_type:"
            f"{type(report).__name__}"
        )
    if not report.issues:
        return f"[{report.scope}] startup config validation passed"
    return "\n".join(
        f"[{issue.severity}] {issue.scope}.{issue.code}: {issue.message}"
        for issue in report.issues
    )


def raise_for_startup_validation_errors(
    report: StartupValidationReport,
) -> None:
    if not isinstance(report, StartupValidationReport):
        raise ValueError(
            "invalid_startup_validation_report_type:"
            f"{type(report).__name__}"
        )
    if report.has_errors:
        raise StartupConfigValidationError(report)


def _shared_issue_from_env_layout_error(exc: ValueError) -> StartupValidationIssue:
    message = str(exc)
    if message == "env_must_be_mapping":
        return _issue(
            scope="shared",
            severity="error",
            code="invalid_env_mapping",
            message="Startup config env source must be a mapping.",
        )
    if message.startswith("invalid_env_value_type:STATE_DB_PATH:"):
        return _issue(
            scope="shared",
            severity="error",
            code="invalid_state_db_path_env_value",
            message="STATE_DB_PATH must resolve from a string env value.",
        )
    if message.startswith("invalid_env_value_type:BOT_STATE_DIR:"):
        return _issue(
            scope="shared",
            severity="error",
            code="invalid_bot_state_dir_env_value",
            message="BOT_STATE_DIR must resolve from a string env value.",
        )
    if message.startswith("invalid_env_value_type:OBS_LOG_PATH:"):
        return _issue(
            scope="shared",
            severity="error",
            code="invalid_obs_log_path_env_value",
            message="OBS_LOG_PATH must resolve from a string env value.",
        )
    if message.startswith("invalid_env_value_type:LOG_LEVEL:"):
        return _issue(
            scope="shared",
            severity="error",
            code="invalid_log_level_env_value",
            message="LOG_LEVEL must resolve from a string env value.",
        )
    return _issue(
        scope="shared",
        severity="error",
        code="invalid_shared_runtime_env",
        message=f"Shared runtime env is invalid: {message}",
    )


def validate_shared_runtime_config(
    env: Mapping[str, str] | None,
) -> StartupValidationReport:
    issues: list[StartupValidationIssue] = []
    try:
        config = SharedRuntimeEnvConfig.from_env(env)
    except ValueError as exc:
        issues.append(_shared_issue_from_env_layout_error(exc))
        return StartupValidationReport(scope="shared", issues=tuple(issues))

    if (
        config.log_level is not None
        and config.log_level not in VALID_LOG_LEVEL_NAMES
    ):
        issues.append(
            _issue(
                scope="shared",
                severity="warning",
                code="unknown_log_level",
                message=(
                    f"LOG_LEVEL `{config.log_level}` is not a standard logging "
                    "level; startup logging will fall back to INFO semantics."
                ),
            )
        )

    return StartupValidationReport(scope="shared", issues=tuple(issues))


def _validate_legacy_bootstrap_warnings(
    config: BotRuntimeEnvConfig,
) -> tuple[StartupValidationIssue, ...]:
    if not isinstance(config, BotRuntimeEnvConfig):
        raise ValueError(
            "invalid_bot_runtime_env_config_type:"
            f"{type(config).__name__}"
        )
    issues: list[StartupValidationIssue] = []
    repo_path = config.legacy.repo_path
    if repo_path is not None:
        try:
            if not repo_path.exists():
                issues.append(
                    _issue(
                        scope="bot",
                        severity="warning",
                        code="legacy_repo_path_missing",
                        message=(
                            f"Legacy REPO_PATH `{repo_path}` does not exist and "
                            "cannot seed a single-project runtime."
                        ),
                    )
                )
            elif not repo_path.is_dir():
                issues.append(
                    _issue(
                        scope="bot",
                        severity="warning",
                        code="legacy_repo_path_not_directory",
                        message=(
                            f"Legacy REPO_PATH `{repo_path}` is not a directory "
                            "and cannot seed a single-project runtime."
                        ),
                    )
                )
            elif not (repo_path / ".git").exists():
                issues.append(
                    _issue(
                        scope="bot",
                        severity="warning",
                        code="legacy_repo_path_not_git",
                        message=(
                            f"Legacy REPO_PATH `{repo_path}` is not a git repo "
                            "and cannot seed a single-project runtime."
                        ),
                    )
                )
        except OSError as exc:
            issues.append(
                _issue(
                    scope="bot",
                    severity="warning",
                    code="legacy_repo_path_unreadable",
                    message=(
                        f"Legacy REPO_PATH `{repo_path}` could not be checked: "
                        f"{exc}"
                    ),
                )
            )

    worktree_root = config.legacy.worktree_root
    if worktree_root is not None:
        try:
            if worktree_root.exists() and not worktree_root.is_dir():
                issues.append(
                    _issue(
                        scope="bot",
                        severity="warning",
                        code="legacy_worktree_root_not_directory",
                        message=(
                            f"Legacy WORKTREE_ROOT `{worktree_root}` exists but "
                            "is not a directory."
                        ),
                    )
                )
        except OSError as exc:
            issues.append(
                _issue(
                    scope="bot",
                    severity="warning",
                    code="legacy_worktree_root_unreadable",
                    message=(
                        f"Legacy WORKTREE_ROOT `{worktree_root}` could not be "
                        f"checked: {exc}"
                    ),
                )
            )

    return tuple(issues)


def _parse_owner_chat_ids(raw: str) -> frozenset[int]:
    if not isinstance(raw, str):
        raise ValueError("owner_chat_id_must_be_string")
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError("empty_owner_chat_id")
    out: set[int] = set()
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError(f"invalid_owner_chat_id:{part}") from exc
        if value <= 0:
            raise ValueError(f"non_positive_owner_chat_id:{value}")
        out.add(value)
    return frozenset(out)


def _bot_issue_from_runtime_spec_error(
    exc: ValueError,
) -> StartupValidationIssue:
    message = str(exc)
    if message.startswith("telegram_agent_tokens_invalid:"):
        return _issue(
            scope="bot",
            severity="error",
            code="invalid_telegram_agent_tokens",
            message=(
                "TELEGRAM_AGENT_TOKENS is present but syntactically invalid: "
                f"{message.removeprefix('telegram_agent_tokens_invalid:')}"
            ),
        )
    if message.startswith("telegram_agent_token_env_missing:"):
        token_env_key = message.removeprefix("telegram_agent_token_env_missing:")
        return _issue(
            scope="bot",
            severity="error",
            code="missing_referenced_bot_token_env",
            message=(
                f"TELEGRAM_AGENT_TOKENS references `{token_env_key}`, but that "
                "token env key is missing."
            ),
        )
    if message.startswith("telegram_agent_token_empty:"):
        token_env_key = message.removeprefix("telegram_agent_token_empty:")
        return _issue(
            scope="bot",
            severity="error",
            code="missing_referenced_bot_token_env",
            message=(
                f"TELEGRAM_AGENT_TOKENS references `{token_env_key}`, but that "
                "token env key is empty."
            ),
        )
    if message == "telegram_agent_tokens_missing_coordinator_agent":
        return _issue(
            scope="bot",
            severity="error",
            code="invalid_telegram_agent_tokens",
            message=(
                "TELEGRAM_AGENT_TOKENS must include coordinator_agent for "
                "multi-bot startup."
            ),
        )
    return _issue(
        scope="bot",
        severity="error",
        code="invalid_telegram_agent_tokens",
        message=f"Multi-bot startup contract is invalid: {message}",
    )


def validate_bot_startup_config(
    env: Mapping[str, str] | None,
) -> StartupValidationReport:
    resolved_env = os.environ if env is None else env
    shared_report = validate_shared_runtime_config(env)
    issues = list(shared_report.issues)

    try:
        config = BotRuntimeEnvConfig.from_env(env)
    except ValueError as exc:
        issues.append(
            _issue(
                scope="bot",
                severity="error",
                code="invalid_bot_runtime_env",
                message=f"Bot runtime env is invalid: {exc}",
            )
        )
        return StartupValidationReport(scope="bot", issues=tuple(issues))

    if config.telegram_owner_chat_id is None:
        issues.append(
            _issue(
                scope="bot",
                severity="error",
                code="missing_telegram_owner_chat_id",
                message="TELEGRAM_OWNER_CHAT_ID is required for bot startup.",
            )
        )
    else:
        try:
            _parse_owner_chat_ids(config.telegram_owner_chat_id)
        except ValueError as exc:
            issues.append(
                _issue(
                    scope="bot",
                    severity="error",
                    code="invalid_telegram_owner_chat_id",
                    message=(
                        "TELEGRAM_OWNER_CHAT_ID must match the existing owner "
                        f"chat id parsing rules: {exc}"
                    ),
                )
            )

    if config.telegram_agent_tokens is not None:
        try:
            runtime_spec = build_multi_bot_runtime_spec(resolved_env)
        except ValueError as exc:
            issues.append(_bot_issue_from_runtime_spec_error(exc))
        else:
            if runtime_spec is None:
                issues.append(
                    _issue(
                        scope="bot",
                        severity="error",
                        code="missing_bot_identity_startup_path",
                        message=(
                            "Bot startup needs a usable TELEGRAM_AGENT_TOKENS "
                            "mapping or TELEGRAM_BOT_TOKEN."
                        ),
                    )
                )
    elif not config.has_single_bot_token:
        issues.append(
            _issue(
                scope="bot",
                severity="error",
                code="missing_bot_identity_startup_path",
                message=(
                    "Bot startup needs TELEGRAM_BOT_TOKEN for single-bot "
                    "compatibility or TELEGRAM_AGENT_TOKENS for multi-bot "
                    "startup."
                ),
            )
        )

    if config.bot_cost_threshold_usd_raw is not None:
        raw_threshold = config.bot_cost_threshold_usd_raw
        try:
            parsed_threshold = float(raw_threshold)
        except ValueError:
            issues.append(
                _issue(
                    scope="bot",
                    severity="error",
                    code="invalid_bot_cost_threshold_usd",
                    message=(
                        "BOT_COST_THRESHOLD_USD must be a valid float when set."
                    ),
                )
            )
        else:
            if parsed_threshold < 0:
                issues.append(
                    _issue(
                        scope="bot",
                        severity="error",
                        code="invalid_bot_cost_threshold_usd",
                        message=(
                            "BOT_COST_THRESHOLD_USD must be >= 0 for current "
                            "confirmation-gate semantics."
                        ),
                    )
                )

    issues.extend(_validate_legacy_bootstrap_warnings(config))
    return StartupValidationReport(scope="bot", issues=tuple(issues))


def validate_web_startup_config(
    env: Mapping[str, str] | None,
) -> StartupValidationReport:
    shared_report = validate_shared_runtime_config(env)
    issues = list(shared_report.issues)

    try:
        WebRuntimeEnvConfig.from_env(env)
    except ValueError as exc:
        issues.append(
            _issue(
                scope="web",
                severity="error",
                code="invalid_web_runtime_env",
                message=f"Web runtime env is invalid: {exc}",
            )
        )

    return StartupValidationReport(scope="web", issues=tuple(issues))
