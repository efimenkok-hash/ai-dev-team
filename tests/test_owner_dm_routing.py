import pytest

from core.owner_dm_routing import OwnerDmRoutingContext, OwnerDmRoutingService
from core.telegram_bridge import IncomingMessage


def _msg(
    *,
    chat_id: int = 101,
    user_id: int = 101,
    incoming_bot_role: str | None = "writer_agent",
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        message_id=1,
        text="привет",
        incoming_bot_role=incoming_bot_role,
    )


def test_owner_dm_routing_context_happy_path():
    context = OwnerDmRoutingContext(
        chat_id=101,
        user_id=101,
        incoming_bot_role="writer_agent",
    )

    assert context.incoming_bot_role == "writer_agent"


@pytest.mark.parametrize("bad", [0, -1, True, "101"])
def test_owner_dm_routing_context_rejects_bad_chat_id(bad):
    with pytest.raises(ValueError, match="invalid_owner_dm_chat_id"):
        OwnerDmRoutingContext(
            chat_id=bad,  # type: ignore[arg-type]
            user_id=101,
            incoming_bot_role="writer_agent",
        )


@pytest.mark.parametrize("bad", [0, -1, True, "101"])
def test_owner_dm_routing_context_rejects_bad_user_id(bad):
    with pytest.raises(ValueError, match="invalid_owner_dm_user_id"):
        OwnerDmRoutingContext(
            chat_id=101,
            user_id=bad,  # type: ignore[arg-type]
            incoming_bot_role="writer_agent",
        )


def test_owner_dm_routing_context_rejects_non_private_shape():
    with pytest.raises(
        ValueError,
        match="owner_dm_requires_private_chat_shape:101!=202",
    ):
        OwnerDmRoutingContext(
            chat_id=101,
            user_id=202,
            incoming_bot_role="writer_agent",
        )


@pytest.mark.parametrize("bad", ["", "  ", "Writer Agent", "writer-agent"])
def test_owner_dm_routing_context_rejects_bad_incoming_bot_role(bad):
    with pytest.raises(
        ValueError,
        match="(empty_incoming_bot_role|invalid_incoming_bot_role:|non_ascii_incoming_bot_role:)",
    ):
        OwnerDmRoutingContext(
            chat_id=101,
            user_id=101,
            incoming_bot_role=bad,
        )


def test_is_owner_dm_message_true_only_for_private_chat_shape():
    service = OwnerDmRoutingService()

    assert service.is_owner_dm_message(_msg()) is True
    assert service.is_owner_dm_message(_msg(chat_id=-100123, user_id=101)) is False
    assert service.is_owner_dm_message(_msg(chat_id=101, user_id=202)) is False


def test_build_context_uses_incoming_bot_role():
    service = OwnerDmRoutingService()

    context = service.build_context(_msg(incoming_bot_role="reviewer_agent"))

    assert context.incoming_bot_role == "reviewer_agent"


def test_resolve_delivery_role_returns_incoming_bot_role_not_sender_role():
    service = OwnerDmRoutingService()
    context = OwnerDmRoutingContext(
        chat_id=101,
        user_id=101,
        incoming_bot_role="writer_agent",
    )

    assert (
        service.resolve_delivery_role(
            context,
            "coordinator_agent",
        )
        == "writer_agent"
    )


def test_owner_dm_routing_service_is_deterministic():
    service = OwnerDmRoutingService()
    msg = _msg(incoming_bot_role="writer_agent")

    assert service.build_context(msg) == service.build_context(msg)
    assert (
        service.resolve_delivery_role(
            service.build_context(msg),
            "architect_agent",
        )
        == "writer_agent"
    )
