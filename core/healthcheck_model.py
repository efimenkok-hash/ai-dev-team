from __future__ import annotations

from dataclasses import dataclass

from core.env_layout import (
    STATE_DB_SOURCE_DEFAULT,
    STATE_DB_SOURCE_ENV,
    STATE_DB_SOURCE_LEGACY,
    BotRuntimeEnvConfig,
)
from core.project_registry import ProjectRegistry
from core.startup_config_validation import (
    StartupValidationReport,
)
from core.state_db import StateDB

VALID_HEALTHCHECK_SCOPES = frozenset({"shared", "bot", "web"})
VALID_HEALTHCHECK_ISSUE_SEVERITIES = frozenset({"warning", "error"})
VALID_HEALTHCHECK_STATUSES = frozenset({"ok", "degraded", "failed"})
VALID_HEALTHCHECK_KINDS = frozenset({"startup", "liveness", "readiness"})


@dataclass(frozen=True)
class HealthcheckIssue:
    scope: str
    severity: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or self.scope not in VALID_HEALTHCHECK_SCOPES:
            raise ValueError(f"invalid_healthcheck_scope:{self.scope!r}")
        if (
            not isinstance(self.severity, str)
            or self.severity not in VALID_HEALTHCHECK_ISSUE_SEVERITIES
        ):
            raise ValueError(f"invalid_healthcheck_issue_severity:{self.severity!r}")
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("empty_healthcheck_issue_code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("empty_healthcheck_issue_message")
        object.__setattr__(self, "code", self.code.strip())
        object.__setattr__(self, "message", self.message.strip())


@dataclass(frozen=True)
class HealthcheckComponent:
    name: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("empty_healthcheck_component_name")
        if not isinstance(self.status, str) or self.status not in VALID_HEALTHCHECK_STATUSES:
            raise ValueError(f"invalid_healthcheck_component_status:{self.status!r}")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("empty_healthcheck_component_detail")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "detail", self.detail.strip())


@dataclass(frozen=True)
class HealthcheckReport:
    scope: str
    kind: str
    status: str
    components: tuple[HealthcheckComponent, ...] = ()
    issues: tuple[HealthcheckIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or self.scope not in VALID_HEALTHCHECK_SCOPES:
            raise ValueError(f"invalid_healthcheck_scope:{self.scope!r}")
        if not isinstance(self.kind, str) or self.kind not in VALID_HEALTHCHECK_KINDS:
            raise ValueError(f"invalid_healthcheck_kind:{self.kind!r}")
        if not isinstance(self.status, str) or self.status not in VALID_HEALTHCHECK_STATUSES:
            raise ValueError(f"invalid_healthcheck_status:{self.status!r}")
        if not isinstance(self.components, tuple):
            raise ValueError("healthcheck_components_must_be_tuple")
        if not isinstance(self.issues, tuple):
            raise ValueError("healthcheck_issues_must_be_tuple")
        for component in self.components:
            if not isinstance(component, HealthcheckComponent):
                raise ValueError(
                    "invalid_healthcheck_component_type:"
                    f"{type(component).__name__}"
                )
        for issue in self.issues:
            if not isinstance(issue, HealthcheckIssue):
                raise ValueError(
                    "invalid_healthcheck_issue_type:"
                    f"{type(issue).__name__}"
                )

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_degraded(self) -> bool:
        return self.status == "degraded"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"


def _issue(
    *,
    scope: str,
    severity: str,
    code: str,
    message: str,
) -> HealthcheckIssue:
    return HealthcheckIssue(
        scope=scope,
        severity=severity,
        code=code,
        message=message,
    )


def _component(
    *,
    name: str,
    status: str,
    detail: str,
) -> HealthcheckComponent:
    return HealthcheckComponent(
        name=name,
        status=status,
        detail=detail,
    )


def _validate_startup_validation_report(
    report: StartupValidationReport,
) -> StartupValidationReport:
    if not isinstance(report, StartupValidationReport):
        raise ValueError(
            "invalid_startup_validation_report_type:"
            f"{type(report).__name__}"
        )
    return report


def build_shared_health_issues_from_startup_validation(
    report: StartupValidationReport,
) -> tuple[HealthcheckIssue, ...]:
    validated_report = _validate_startup_validation_report(report)
    return tuple(
        _issue(
            scope=issue.scope,
            severity=issue.severity,
            code=issue.code,
            message=issue.message,
        )
        for issue in validated_report.issues
    )


def derive_health_status(
    *,
    components: tuple[HealthcheckComponent, ...] = (),
    issues: tuple[HealthcheckIssue, ...] = (),
) -> str:
    if not isinstance(components, tuple):
        raise ValueError("healthcheck_components_must_be_tuple")
    if not isinstance(issues, tuple):
        raise ValueError("healthcheck_issues_must_be_tuple")
    for component in components:
        if not isinstance(component, HealthcheckComponent):
            raise ValueError(
                "invalid_healthcheck_component_type:"
                f"{type(component).__name__}"
            )
    for issue in issues:
        if not isinstance(issue, HealthcheckIssue):
            raise ValueError(
                "invalid_healthcheck_issue_type:"
                f"{type(issue).__name__}"
            )

    if any(issue.severity == "error" for issue in issues):
        return "failed"
    if any(component.status == "failed" for component in components):
        return "failed"
    if any(issue.severity == "warning" for issue in issues):
        return "degraded"
    if any(component.status == "degraded" for component in components):
        return "degraded"
    return "ok"


def _build_startup_validation_component(
    report: StartupValidationReport,
) -> HealthcheckComponent:
    validated_report = _validate_startup_validation_report(report)
    error_count = sum(
        1
        for issue in validated_report.issues
        if issue.severity == "error"
    )
    warning_count = sum(
        1
        for issue in validated_report.issues
        if issue.severity == "warning"
    )
    if error_count:
        return _component(
            name="startup_validation",
            status="failed",
            detail=(
                "Startup validation has "
                f"{error_count} error(s) and {warning_count} warning(s)."
            ),
        )
    if warning_count:
        return _component(
            name="startup_validation",
            status="degraded",
            detail=f"Startup validation has {warning_count} warning(s).",
        )
    return _component(
        name="startup_validation",
        status="ok",
        detail="Startup validation passed.",
    )


def _build_bot_identity_component(
    env_config: BotRuntimeEnvConfig,
    startup_validation_report: StartupValidationReport,
) -> HealthcheckComponent:
    if not isinstance(env_config, BotRuntimeEnvConfig):
        raise ValueError(
            "invalid_bot_runtime_env_config_type:"
            f"{type(env_config).__name__}"
        )
    validated_report = _validate_startup_validation_report(startup_validation_report)
    blocking_codes = {
        "missing_bot_identity_startup_path",
        "invalid_telegram_agent_tokens",
        "missing_referenced_bot_token_env",
    }
    has_blocking_identity_error = any(
        issue.code in blocking_codes and issue.severity == "error"
        for issue in validated_report.issues
    )
    if has_blocking_identity_error:
        return _component(
            name="bot_identity_startup_path",
            status="failed",
            detail="No usable bot identity startup path is configured.",
        )
    if env_config.has_multi_bot_contract:
        return _component(
            name="bot_identity_startup_path",
            status="ok",
            detail="Using TELEGRAM_AGENT_TOKENS multi-bot startup path.",
        )
    if env_config.has_single_bot_token:
        return _component(
            name="bot_identity_startup_path",
            status="ok",
            detail="Using TELEGRAM_BOT_TOKEN single-bot compatibility startup path.",
        )
    return _component(
        name="bot_identity_startup_path",
        status="failed",
        detail="No usable bot identity startup path is configured.",
    )


def _build_shared_persistence_component(
    env_config: BotRuntimeEnvConfig,
    *,
    state_db_fallback_in_use: bool = False,
) -> tuple[HealthcheckComponent, tuple[HealthcheckIssue, ...]]:
    if not isinstance(env_config, BotRuntimeEnvConfig):
        raise ValueError(
            "invalid_bot_runtime_env_config_type:"
            f"{type(env_config).__name__}"
        )
    shared = env_config.shared
    if state_db_fallback_in_use:
        return (
            _component(
                name="state_persistence",
                status="degraded",
                detail="Using import-safe fallback state DB path for local persistence.",
            ),
            (
                _issue(
                    scope="shared",
                    severity="warning",
                    code="state_db_import_safe_fallback_in_use",
                    message="Using import-safe fallback state DB path for local persistence.",
                ),
            ),
        )
    if shared.state_db_source == STATE_DB_SOURCE_LEGACY:
        return (
            _component(
                name="state_persistence",
                status="degraded",
                detail="Using legacy BOT_STATE_DIR fallback for persisted state path.",
            ),
            (
                _issue(
                    scope="shared",
                    severity="warning",
                    code="state_db_legacy_fallback_in_use",
                    message="Using legacy BOT_STATE_DIR fallback for persisted state path.",
                ),
            ),
        )
    if shared.state_db_source == STATE_DB_SOURCE_ENV:
        return (
            _component(
                name="state_persistence",
                status="ok",
                detail="Using canonical STATE_DB_PATH for persisted state.",
            ),
            (),
        )
    if shared.state_db_source == STATE_DB_SOURCE_DEFAULT:
        return (
            _component(
                name="state_persistence",
                status="ok",
                detail="Using default home state DB path for local persistence.",
            ),
            (),
        )
    raise ValueError(f"invalid_state_db_source:{shared.state_db_source!r}")


def _build_legacy_bootstrap_component(
    env_config: BotRuntimeEnvConfig,
    health_issues: tuple[HealthcheckIssue, ...],
) -> HealthcheckComponent:
    if not isinstance(env_config, BotRuntimeEnvConfig):
        raise ValueError(
            "invalid_bot_runtime_env_config_type:"
            f"{type(env_config).__name__}"
        )
    legacy_warning_codes = tuple(
        issue.code
        for issue in health_issues
        if issue.scope == "bot" and issue.code.startswith("legacy_")
    )
    if legacy_warning_codes:
        return _component(
            name="legacy_bootstrap",
            status="degraded",
            detail=(
                "Legacy bootstrap compatibility warnings are present: "
                + ", ".join(legacy_warning_codes)
            ),
        )
    if (
        env_config.legacy.repo_path is not None
        or env_config.legacy.worktree_root is not None
    ):
        return _component(
            name="legacy_bootstrap",
            status="ok",
            detail="Legacy bootstrap compatibility keys are present but non-blocking.",
        )
    return _component(
        name="legacy_bootstrap",
        status="ok",
        detail="Legacy bootstrap compatibility keys are not in active use.",
    )


def build_bot_startup_healthcheck_report(
    *,
    startup_validation_report: StartupValidationReport,
    env_config: BotRuntimeEnvConfig,
) -> HealthcheckReport:
    validated_report = _validate_startup_validation_report(startup_validation_report)
    if not isinstance(env_config, BotRuntimeEnvConfig):
        raise ValueError(
            "invalid_bot_runtime_env_config_type:"
            f"{type(env_config).__name__}"
        )

    issues = list(build_shared_health_issues_from_startup_validation(validated_report))
    startup_component = _build_startup_validation_component(validated_report)
    bot_identity_component = _build_bot_identity_component(
        env_config,
        validated_report,
    )
    persistence_component, persistence_issues = _build_shared_persistence_component(
        env_config,
    )
    issues.extend(persistence_issues)
    legacy_component = _build_legacy_bootstrap_component(
        env_config,
        tuple(issues),
    )
    components = (
        startup_component,
        bot_identity_component,
        persistence_component,
        legacy_component,
    )
    status = derive_health_status(
        components=components,
        issues=tuple(issues),
    )
    return HealthcheckReport(
        scope="bot",
        kind="startup",
        status=status,
        components=components,
        issues=tuple(issues),
    )


def build_web_liveness_healthcheck_report(
    *,
    state_db: StateDB | None,
    state_db_fallback_in_use: bool = False,
) -> HealthcheckReport:
    issues: list[HealthcheckIssue] = []
    components: list[HealthcheckComponent] = []

    if not isinstance(state_db, StateDB):
        issues.append(
            _issue(
                scope="shared",
                severity="error",
                code="missing_state_db",
                message="Web runtime state DB object is unavailable.",
            )
        )
        components.append(
            _component(
                name="state_db",
                status="failed",
                detail="State DB runtime object is unavailable.",
            )
        )
    else:
        try:
            schema_version = state_db.schema_version()
        except Exception as exc:
            issues.append(
                _issue(
                    scope="shared",
                    severity="error",
                    code="state_db_schema_unavailable",
                    message=f"State DB schema version could not be read: {exc}",
                )
            )
            components.append(
                _component(
                    name="state_db",
                    status="failed",
                    detail=f"State DB schema version could not be read: {exc}",
                )
            )
        else:
            if state_db_fallback_in_use:
                issues.append(
                    _issue(
                        scope="shared",
                        severity="warning",
                        code="state_db_import_safe_fallback_in_use",
                        message=(
                            "Web runtime is using import-safe fallback state DB path."
                        ),
                    )
                )
                components.append(
                    _component(
                        name="state_db",
                        status="degraded",
                        detail=(
                            "State DB is available via import-safe fallback path "
                            f"with schema version {schema_version}."
                        ),
                    )
                )
            else:
                components.append(
                    _component(
                        name="state_db",
                        status="ok",
                        detail=(
                            "State DB is available with schema version "
                            f"{schema_version}."
                        ),
                    )
                )

    report_issues = tuple(issues)
    report_components = tuple(components)
    return HealthcheckReport(
        scope="web",
        kind="liveness",
        status=derive_health_status(
            components=report_components,
            issues=report_issues,
        ),
        components=report_components,
        issues=report_issues,
    )


def build_web_readiness_healthcheck_report(
    *,
    startup_validation_report: StartupValidationReport,
    state_db: StateDB | None,
    project_registry: ProjectRegistry | None,
    state_db_fallback_in_use: bool = False,
) -> HealthcheckReport:
    validated_report = _validate_startup_validation_report(startup_validation_report)
    liveness_report = build_web_liveness_healthcheck_report(
        state_db=state_db,
        state_db_fallback_in_use=state_db_fallback_in_use,
    )
    issues = list(liveness_report.issues)
    issues.extend(build_shared_health_issues_from_startup_validation(validated_report))
    components = list(liveness_report.components)
    components.append(_build_startup_validation_component(validated_report))

    if not isinstance(project_registry, ProjectRegistry):
        issues.append(
            _issue(
                scope="web",
                severity="error",
                code="missing_project_registry",
                message="Web runtime project registry is unavailable.",
            )
        )
        components.append(
            _component(
                name="project_registry",
                status="failed",
                detail="Project registry runtime object is unavailable.",
            )
        )
    else:
        components.append(
            _component(
                name="project_registry",
                status="ok",
                detail="Project registry is ready for project-scoped reads.",
            )
        )

    report_issues = tuple(issues)
    report_components = tuple(components)
    return HealthcheckReport(
        scope="web",
        kind="readiness",
        status=derive_health_status(
            components=report_components,
            issues=report_issues,
        ),
        components=report_components,
        issues=report_issues,
    )
