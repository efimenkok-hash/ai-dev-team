from __future__ import annotations

from core.env_layout import BotRuntimeEnvConfig
from core.healthcheck_model import (
    HealthcheckIssue,
    build_bot_startup_healthcheck_report,
    build_web_liveness_healthcheck_report,
    build_web_readiness_healthcheck_report,
    derive_health_status,
)
from core.project_registry import ProjectRegistry
from core.startup_config_validation import (
    StartupValidationIssue,
    StartupValidationReport,
    validate_bot_startup_config,
)
from core.state_db import StateDB


def _warning_issue() -> HealthcheckIssue:
    return HealthcheckIssue(
        scope="shared",
        severity="warning",
        code="test_warning",
        message="warning",
    )


def _error_issue() -> HealthcheckIssue:
    return HealthcheckIssue(
        scope="shared",
        severity="error",
        code="test_error",
        message="error",
    )


def _startup_error_report() -> StartupValidationReport:
    return StartupValidationReport(
        scope="web",
        issues=(
            StartupValidationIssue(
                scope="web",
                severity="error",
                code="invalid_web_runtime_env",
                message="Web runtime env is invalid.",
            ),
        ),
    )


def test_derive_health_status_keeps_warning_as_degraded_not_failed():
    assert derive_health_status(issues=(_warning_issue(),)) == "degraded"
    assert derive_health_status(issues=(_error_issue(),)) == "failed"
    assert derive_health_status() == "ok"


def test_build_bot_startup_healthcheck_report_is_ok_for_valid_single_bot_path():
    env = {
        "STATE_DB_PATH": "/tmp/state.db",
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "TELEGRAM_BOT_TOKEN": "123:legacy",
    }

    validation_report = validate_bot_startup_config(env)
    bot_env = BotRuntimeEnvConfig.from_env(env)
    health_report = build_bot_startup_healthcheck_report(
        startup_validation_report=validation_report,
        env_config=bot_env,
    )

    assert health_report.scope == "bot"
    assert health_report.kind == "startup"
    assert health_report.is_ok is True
    assert health_report.status == "ok"
    assert all(
        "reachable" not in component.detail.lower()
        and "live" not in component.detail.lower()
        for component in health_report.components
    )


def test_build_bot_startup_healthcheck_report_is_degraded_for_legacy_warnings(
    tmp_path,
):
    env = {
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "TELEGRAM_BOT_TOKEN": "123:legacy",
        "REPO_PATH": str(tmp_path / "missing-repo"),
    }

    validation_report = validate_bot_startup_config(env)
    bot_env = BotRuntimeEnvConfig.from_env(env)
    health_report = build_bot_startup_healthcheck_report(
        startup_validation_report=validation_report,
        env_config=bot_env,
    )

    assert validation_report.has_warnings is True
    assert health_report.is_degraded is True
    assert any(issue.code == "legacy_repo_path_missing" for issue in health_report.issues)


def test_build_bot_startup_healthcheck_report_is_failed_for_fatal_validation():
    env = {"TELEGRAM_BOT_TOKEN": "123:legacy"}

    validation_report = validate_bot_startup_config(env)
    bot_env = BotRuntimeEnvConfig.from_env(env)
    health_report = build_bot_startup_healthcheck_report(
        startup_validation_report=validation_report,
        env_config=bot_env,
    )

    assert validation_report.has_errors is True
    assert health_report.is_failed is True
    assert any(
        issue.code == "missing_telegram_owner_chat_id"
        for issue in health_report.issues
    )


def test_build_web_healthcheck_reports_are_truthful_for_normal_path(tmp_path):
    state_db = StateDB(tmp_path / "state.db")
    project_registry = ProjectRegistry(state_db)
    startup_validation_report = StartupValidationReport(scope="web")

    liveness_report = build_web_liveness_healthcheck_report(state_db=state_db)
    readiness_report = build_web_readiness_healthcheck_report(
        startup_validation_report=startup_validation_report,
        state_db=state_db,
        project_registry=project_registry,
    )

    assert liveness_report.is_ok is True
    assert readiness_report.is_ok is True


def test_web_liveness_keeps_state_db_fallback_as_degraded_not_failed(tmp_path):
    state_db = StateDB(tmp_path / "fallback-state.db")

    liveness_report = build_web_liveness_healthcheck_report(
        state_db=state_db,
        state_db_fallback_in_use=True,
    )

    assert liveness_report.is_degraded is True
    assert liveness_report.is_failed is False
    assert any(
        issue.code == "state_db_import_safe_fallback_in_use"
        for issue in liveness_report.issues
    )


def test_web_healthcheck_reports_fail_when_critical_runtime_dependency_missing(
    tmp_path,
):
    state_db = StateDB(tmp_path / "state.db")
    startup_validation_report = StartupValidationReport(scope="web")

    liveness_report = build_web_liveness_healthcheck_report(state_db=None)
    readiness_report = build_web_readiness_healthcheck_report(
        startup_validation_report=startup_validation_report,
        state_db=state_db,
        project_registry=None,
    )

    assert liveness_report.is_failed is True
    assert readiness_report.is_failed is True
    assert any(issue.code == "missing_state_db" for issue in liveness_report.issues)
    assert any(
        issue.code == "missing_project_registry"
        for issue in readiness_report.issues
    )


def test_web_readiness_healthcheck_fails_on_startup_validation_errors(tmp_path):
    state_db = StateDB(tmp_path / "state.db")
    project_registry = ProjectRegistry(state_db)

    readiness_report = build_web_readiness_healthcheck_report(
        startup_validation_report=_startup_error_report(),
        state_db=state_db,
        project_registry=project_registry,
    )

    assert readiness_report.is_failed is True
    assert any(
        issue.code == "invalid_web_runtime_env"
        for issue in readiness_report.issues
    )
