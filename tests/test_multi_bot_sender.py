import pytest

from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_runtime import BotIdentity
from core.multi_bot_sender import (
    MultiBotOutboundSender,
    PerRoleOutboundSender,
    RoleBoundSender,
)
from core.telegram_bridge import OutgoingEnvelope, OutgoingMessage


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


def _envelope(
    *,
    role: str,
    text: str = "hello",
    delivery_role: str | None = None,
) -> OutgoingEnvelope:
    return OutgoingEnvelope(
        message=OutgoingMessage(chat_id=1, text=text),
        sender_role=role,
        delivery_role=delivery_role,
    )


class _CapturingEnvelopeSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingEnvelope] = []

    def __call__(self, envelope: OutgoingEnvelope) -> None:
        self.sent.append(envelope)


def test_role_bound_sender_happy_path():
    sender = _CapturingEnvelopeSender()
    bound = RoleBoundSender(
        identity=_identity(),
        send_envelope=sender,
    )

    assert bound.identity.agent_role == COORDINATOR_ROLE


def test_per_role_outbound_sender_happy_path():
    sender = _CapturingEnvelopeSender()
    config = PerRoleOutboundSender(
        primary_role=COORDINATOR_ROLE,
        senders_by_role={
            COORDINATOR_ROLE: RoleBoundSender(
                identity=_identity(),
                send_envelope=sender,
            )
        },
    )

    assert tuple(config.senders_by_role.keys()) == (COORDINATOR_ROLE,)


def test_per_role_outbound_sender_rejects_empty_map():
    with pytest.raises(ValueError, match="empty_outbound_sender_map"):
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={},
        )


def test_per_role_outbound_sender_rejects_non_coordinator_primary_role():
    sender = _CapturingEnvelopeSender()
    with pytest.raises(
        ValueError,
        match="outbound_primary_role_must_be_coordinator_agent:writer_agent",
    ):
        PerRoleOutboundSender(
            primary_role="writer_agent",
            senders_by_role={
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=sender,
                )
            },
        )


def test_per_role_outbound_sender_rejects_missing_primary_sender():
    sender = _CapturingEnvelopeSender()
    with pytest.raises(
        ValueError,
        match="missing_primary_outbound_sender:coordinator_agent",
    ):
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=sender,
                )
            },
        )


def test_per_role_outbound_sender_rejects_role_identity_mismatch():
    sender = _CapturingEnvelopeSender()
    with pytest.raises(
        ValueError,
        match="outbound_sender_role_identity_mismatch:writer_agent!=coordinator_agent",
    ):
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=sender,
                ),
                "writer_agent": RoleBoundSender(
                    identity=_identity(COORDINATOR_ROLE, token="456:coord"),
                    send_envelope=sender,
                ),
            },
        )


def test_multi_bot_outbound_sender_exact_role_match_uses_matching_sender():
    coordinator_sender = _CapturingEnvelopeSender()
    writer_sender = _CapturingEnvelopeSender()
    sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=writer_sender,
                ),
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=coordinator_sender,
                ),
            },
        )
    )

    used_role = sender.send(_envelope(role="writer_agent", text="Writer: draft"))

    assert used_role == "writer_agent"
    assert len(writer_sender.sent) == 1
    assert writer_sender.sent[0].text == "Writer: draft"
    assert not coordinator_sender.sent


def test_multi_bot_outbound_sender_unknown_role_falls_back_to_coordinator():
    coordinator_sender = _CapturingEnvelopeSender()
    writer_sender = _CapturingEnvelopeSender()
    sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=writer_sender,
                ),
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=coordinator_sender,
                ),
            },
        )
    )

    used_role = sender.send(_envelope(role="ghost_agent", text="Architect: keep text"))

    assert used_role == COORDINATOR_ROLE
    assert len(coordinator_sender.sent) == 1
    assert coordinator_sender.sent[0].text == "Architect: keep text"
    assert not writer_sender.sent


def test_multi_bot_outbound_sender_delivery_role_none_keeps_old_behavior():
    coordinator_sender = _CapturingEnvelopeSender()
    writer_sender = _CapturingEnvelopeSender()
    sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=writer_sender,
                ),
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=coordinator_sender,
                ),
            },
        )
    )

    used_role = sender.send(
        _envelope(
            role="writer_agent",
            delivery_role=None,
            text="Writer: keep sender role routing",
        )
    )

    assert used_role == "writer_agent"
    assert len(writer_sender.sent) == 1
    assert not coordinator_sender.sent


def test_multi_bot_outbound_sender_delivery_role_overrides_sender_role():
    coordinator_sender = _CapturingEnvelopeSender()
    writer_sender = _CapturingEnvelopeSender()
    sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=writer_sender,
                ),
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=coordinator_sender,
                ),
            },
        )
    )

    used_role = sender.send(
        _envelope(
            role=COORDINATOR_ROLE,
            delivery_role="writer_agent",
            text="Координатор: transport through writer",
        )
    )

    assert used_role == "writer_agent"
    assert len(writer_sender.sent) == 1
    assert writer_sender.sent[0].sender_role == COORDINATOR_ROLE
    assert writer_sender.sent[0].delivery_role == "writer_agent"
    assert not coordinator_sender.sent


def test_multi_bot_outbound_sender_unknown_delivery_role_falls_back_to_coordinator():
    coordinator_sender = _CapturingEnvelopeSender()
    writer_sender = _CapturingEnvelopeSender()
    sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=writer_sender,
                ),
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=coordinator_sender,
                ),
            },
        )
    )

    used_role = sender.send(
        _envelope(
            role="writer_agent",
            delivery_role="ghost_agent",
            text="Writer: keep text on fallback",
        )
    )

    assert used_role == COORDINATOR_ROLE
    assert len(coordinator_sender.sent) == 1
    assert coordinator_sender.sent[0].text == "Writer: keep text on fallback"
    assert not writer_sender.sent


def test_multi_bot_outbound_sender_enabled_roles_are_deterministic():
    sender = _CapturingEnvelopeSender()
    outbound = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                "writer_agent": RoleBoundSender(
                    identity=_identity("writer_agent", token="456:writer"),
                    send_envelope=sender,
                ),
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=sender,
                ),
            },
        )
    )

    assert outbound.enabled_roles() == (
        COORDINATOR_ROLE,
        "writer_agent",
    )


def test_multi_bot_outbound_sender_resolve_sender_rejects_unknown_role():
    sender = _CapturingEnvelopeSender()
    outbound = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                COORDINATOR_ROLE: RoleBoundSender(
                    identity=_identity(),
                    send_envelope=sender,
                )
            },
        )
    )

    with pytest.raises(
        ValueError,
        match="unknown_outbound_sender_role:ghost_agent",
    ):
        outbound.resolve_sender("ghost_agent")
