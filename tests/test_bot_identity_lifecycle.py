from __future__ import annotations

import pytest

from core.bot_identity_lifecycle import (
    BotIdentityLifecycleService,
    BotIdentityLifecycleState,
    BotIdentityReachability,
    MultiBotLifecycleReport,
)
from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_runtime import BotIdentity, MultiBotRuntimeSpec, PerRoleBotMap


def _identity(
    role: str = COORDINATOR_ROLE,
    *,
    token: str = "123:token",
) -> BotIdentity:
    return BotIdentity(
        bot_id=role,
        agent_role=role,
        token_env_key=f"TELEGRAM_{role.upper()}_TOKEN",
        token=token,
    )


def _role_map(*identities: BotIdentity) -> PerRoleBotMap:
    return PerRoleBotMap({identity.agent_role: identity for identity in identities})


def _runtime_spec(*identities: BotIdentity, source: str) -> MultiBotRuntimeSpec:
    role_map = _role_map(*identities)
    return MultiBotRuntimeSpec(
        primary_bot=role_map.by_role[COORDINATOR_ROLE],
        role_map=role_map,
        source=source,
    )


def _unreachable_reachability(
    identity: BotIdentity,
    *,
    failure_reason: str | None = None,
) -> BotIdentityReachability:
    return BotIdentityReachability(
        identity=identity,
        bot_user_id=None,
        bot_username=None,
        token_valid=False,
        reachable=False,
        failure_reason=failure_reason,
    )


def _reachable_reachability(identity: BotIdentity) -> BotIdentityReachability:
    return BotIdentityReachability(
        identity=identity,
        bot_user_id=123456,
        bot_username=f"{identity.agent_role}_bot",
        token_valid=True,
        reachable=True,
        failure_reason=None,
    )


def _state(
    identity: BotIdentity,
    *,
    reachability: BotIdentityReachability | None = None,
    application_built: bool = True,
    initialized: bool = False,
    started: bool = False,
    polling_started: bool = False,
    failure_reason: str | None = None,
) -> BotIdentityLifecycleState:
    return BotIdentityLifecycleState(
        identity=identity,
        reachability=reachability or _unreachable_reachability(identity),
        application_built=application_built,
        initialized=initialized,
        started=started,
        polling_started=polling_started,
        failure_reason=failure_reason,
    )


def test_bot_identity_reachability_happy_path():
    reachability = _reachable_reachability(_identity())

    assert reachability.reachable is True
    assert reachability.token_valid is True


def test_bot_identity_reachability_rejects_bad_identity():
    with pytest.raises(ValueError, match="invalid_bot_identity_type:str"):
        BotIdentityReachability(  # type: ignore[arg-type]
            identity="not-identity",
            bot_user_id=None,
            bot_username=None,
            token_valid=False,
            reachable=False,
            failure_reason=None,
        )


def test_bot_identity_reachability_rejects_reachable_without_username():
    with pytest.raises(ValueError, match="reachable_requires_bot_username"):
        BotIdentityReachability(
            identity=_identity(),
            bot_user_id=123,
            bot_username=None,
            token_valid=True,
            reachable=True,
            failure_reason=None,
        )


def test_bot_identity_reachability_rejects_reachable_without_user_id():
    with pytest.raises(ValueError, match="reachable_requires_bot_user_id"):
        BotIdentityReachability(
            identity=_identity(),
            bot_user_id=None,
            bot_username="coord_bot",
            token_valid=True,
            reachable=True,
            failure_reason=None,
        )


def test_bot_identity_reachability_rejects_empty_failure_reason():
    with pytest.raises(ValueError, match="invalid_reachability_failure_reason"):
        BotIdentityReachability(
            identity=_identity(),
            bot_user_id=None,
            bot_username=None,
            token_valid=False,
            reachable=False,
            failure_reason="  ",
        )


def test_bot_identity_lifecycle_state_happy_path():
    state = _state(
        _identity(),
        reachability=_reachable_reachability(_identity()),
        initialized=True,
        started=True,
        polling_started=True,
    )

    assert state.polling_started is True


def test_bot_identity_lifecycle_state_rejects_bad_reachability_type():
    with pytest.raises(
        ValueError,
        match="invalid_bot_identity_reachability_type:str",
    ):
        BotIdentityLifecycleState(  # type: ignore[arg-type]
            identity=_identity(),
            reachability="bad",
            application_built=True,
            initialized=False,
            started=False,
            polling_started=False,
            failure_reason=None,
        )


def test_bot_identity_lifecycle_state_rejects_polling_without_started():
    with pytest.raises(ValueError, match="polling_started_requires_started"):
        _state(
            _identity(),
            reachability=_reachable_reachability(_identity()),
            initialized=True,
            started=False,
            polling_started=True,
        )


def test_bot_identity_lifecycle_state_rejects_started_without_application_built():
    with pytest.raises(ValueError, match="started_requires_application_built"):
        _state(
            _identity(),
            reachability=_reachable_reachability(_identity()),
            application_built=False,
            initialized=False,
            started=True,
        )


def test_bot_identity_lifecycle_state_rejects_role_mismatch():
    with pytest.raises(
        ValueError,
        match="lifecycle_identity_reachability_role_mismatch:writer_agent!=coordinator_agent",
    ):
        BotIdentityLifecycleState(
            identity=_identity("writer_agent", token="456:writer"),
            reachability=_reachable_reachability(_identity()),
            application_built=True,
            initialized=False,
            started=False,
            polling_started=False,
            failure_reason=None,
        )


def test_multi_bot_lifecycle_report_happy_path_legacy():
    identity = _identity()
    report = MultiBotLifecycleReport(
        source="single_token_legacy",
        primary_role=COORDINATOR_ROLE,
        states_by_role={COORDINATOR_ROLE: _state(identity)},
    )

    assert tuple(report.states_by_role.keys()) == (COORDINATOR_ROLE,)


def test_multi_bot_lifecycle_report_happy_path_multi_role():
    coordinator = _identity()
    writer = _identity("writer_agent", token="456:writer")
    report = MultiBotLifecycleReport(
        source="telegram_agent_tokens",
        primary_role=COORDINATOR_ROLE,
        states_by_role={
            "writer_agent": _state(writer),
            COORDINATOR_ROLE: _state(coordinator),
        },
    )

    assert tuple(report.states_by_role.keys()) == (
        COORDINATOR_ROLE,
        "writer_agent",
    )


def test_multi_bot_lifecycle_report_rejects_empty_states():
    with pytest.raises(ValueError, match="empty_lifecycle_states"):
        MultiBotLifecycleReport(
            source="telegram_agent_tokens",
            primary_role=COORDINATOR_ROLE,
            states_by_role={},
        )


def test_multi_bot_lifecycle_report_rejects_missing_primary():
    writer = _identity("writer_agent", token="456:writer")
    with pytest.raises(
        ValueError,
        match="missing_lifecycle_primary_role:coordinator_agent",
    ):
        MultiBotLifecycleReport(
            source="telegram_agent_tokens",
            primary_role=COORDINATOR_ROLE,
            states_by_role={"writer_agent": _state(writer)},
        )


def test_multi_bot_lifecycle_report_rejects_key_role_mismatch():
    with pytest.raises(
        ValueError,
        match="lifecycle_state_role_mismatch:writer_agent!=coordinator_agent",
    ):
        MultiBotLifecycleReport(
            source="telegram_agent_tokens",
            primary_role=COORDINATOR_ROLE,
            states_by_role={"writer_agent": _state(_identity())},
        )


def test_multi_bot_lifecycle_report_normalizes_order_deterministically():
    coordinator = _identity()
    writer = _identity("writer_agent", token="456:writer")
    reviewer = _identity("reviewer_agent", token="789:reviewer")
    report = MultiBotLifecycleReport(
        source="telegram_agent_tokens",
        primary_role=COORDINATOR_ROLE,
        states_by_role={
            "writer_agent": _state(writer),
            COORDINATOR_ROLE: _state(coordinator),
            "reviewer_agent": _state(reviewer),
        },
    )

    assert tuple(report.states_by_role.keys()) == (
        COORDINATOR_ROLE,
        "reviewer_agent",
        "writer_agent",
    )


def test_lifecycle_service_build_initial_report_from_legacy_runtime():
    service = BotIdentityLifecycleService()
    report = service.build_initial_report(
        _runtime_spec(_identity(), source="single_token_legacy")
    )

    state = report.states_by_role[COORDINATOR_ROLE]
    assert state.application_built is True
    assert state.initialized is False
    assert state.reachability.reachable is False


def test_lifecycle_service_build_initial_report_from_multi_runtime():
    service = BotIdentityLifecycleService()
    report = service.build_initial_report(
        _runtime_spec(
            _identity(),
            _identity("writer_agent", token="456:writer"),
            source="telegram_agent_tokens",
        )
    )

    assert tuple(report.states_by_role.keys()) == (
        COORDINATOR_ROLE,
        "writer_agent",
    )


def test_lifecycle_service_mark_reachable_updates_only_target_role():
    service = BotIdentityLifecycleService()
    report = service.build_initial_report(
        _runtime_spec(
            _identity(),
            _identity("writer_agent", token="456:writer"),
            source="telegram_agent_tokens",
        )
    )

    updated = service.mark_reachable(
        report,
        "writer_agent",
        bot_user_id=777,
        bot_username="writer_bot",
    )

    assert updated.states_by_role["writer_agent"].reachability.reachable is True
    assert updated.states_by_role[COORDINATOR_ROLE].reachability.reachable is False


def test_lifecycle_service_mark_initialized_started_and_polling_preserve_state():
    service = BotIdentityLifecycleService()
    report = service.build_initial_report(
        _runtime_spec(_identity(), source="single_token_legacy")
    )
    report = service.mark_reachable(
        report,
        COORDINATOR_ROLE,
        bot_user_id=123,
        bot_username="coord_bot",
    )
    report = service.mark_initialized(report, COORDINATOR_ROLE)
    report = service.mark_started(report, COORDINATOR_ROLE)
    report = service.mark_polling_started(report, COORDINATOR_ROLE)

    state = report.states_by_role[COORDINATOR_ROLE]
    assert state.reachability.reachable is True
    assert state.initialized is True
    assert state.started is True
    assert state.polling_started is True


def test_lifecycle_service_mark_failure_records_truthful_reason():
    service = BotIdentityLifecycleService()
    report = service.build_initial_report(
        _runtime_spec(_identity(), source="single_token_legacy")
    )

    updated = service.mark_failure(
        report,
        COORDINATOR_ROLE,
        "bot_token_invalid:coordinator_agent",
    )

    state = updated.states_by_role[COORDINATOR_ROLE]
    assert state.failure_reason == "bot_token_invalid:coordinator_agent"
    assert (
        state.reachability.failure_reason
        == "bot_token_invalid:coordinator_agent"
    )


def test_lifecycle_service_format_report_is_deterministic():
    service = BotIdentityLifecycleService()
    report = service.build_initial_report(
        _runtime_spec(
            _identity(),
            _identity("writer_agent", token="456:writer"),
            source="telegram_agent_tokens",
        )
    )

    assert service.format_report(report) == service.format_report(report)
