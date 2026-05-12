from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock


class InvalidToken(Exception):
    """Test-only stand-in for Telegram token validation failures."""


@dataclass(frozen=True)
class MockPtbMe:
    id: int
    username: str


@dataclass(frozen=True)
class MockPtbFile:
    payload: bytes

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.payload)


@dataclass(frozen=True)
class MockPtbVoice:
    payload: bytes
    mime_type: str = "audio/ogg"

    async def get_file(self) -> MockPtbFile:
        return MockPtbFile(self.payload)


@dataclass(frozen=True)
class MockPtbPhotoSize:
    payload: bytes

    async def get_file(self) -> MockPtbFile:
        return MockPtbFile(self.payload)


@dataclass(frozen=True)
class MockPtbChat:
    id: int


@dataclass(frozen=True)
class MockPtbUser:
    id: int


@dataclass(frozen=True)
class MockPtbMessage:
    chat: MockPtbChat
    from_user: MockPtbUser
    message_id: int
    text: str | None = None
    caption: str | None = None
    voice: MockPtbVoice | None = None
    photo: tuple[MockPtbPhotoSize, ...] | None = None
    date: datetime | None = None


@dataclass(frozen=True)
class MockPtbUpdate:
    message: MockPtbMessage | None = None


class MockPtbUpdateFactory:
    @staticmethod
    def text(
        *,
        chat_id: int,
        user_id: int,
        text: str,
        message_id: int = 1,
        date: datetime | None = None,
    ) -> MockPtbUpdate:
        return MockPtbUpdate(
            message=MockPtbMessage(
                chat=MockPtbChat(chat_id),
                from_user=MockPtbUser(user_id),
                message_id=message_id,
                text=text,
                date=date or datetime.now(UTC),
            )
        )

    @staticmethod
    def photo(
        *,
        chat_id: int,
        user_id: int,
        payload: bytes,
        message_id: int = 1,
        caption: str | None = None,
        date: datetime | None = None,
    ) -> MockPtbUpdate:
        return MockPtbUpdate(
            message=MockPtbMessage(
                chat=MockPtbChat(chat_id),
                from_user=MockPtbUser(user_id),
                message_id=message_id,
                caption=caption,
                photo=(MockPtbPhotoSize(payload),),
                date=date or datetime.now(UTC),
            )
        )

    @staticmethod
    def voice(
        *,
        chat_id: int,
        user_id: int,
        payload: bytes,
        mime_type: str = "audio/ogg",
        message_id: int = 1,
        caption: str | None = None,
        date: datetime | None = None,
    ) -> MockPtbUpdate:
        return MockPtbUpdate(
            message=MockPtbMessage(
                chat=MockPtbChat(chat_id),
                from_user=MockPtbUser(user_id),
                message_id=message_id,
                caption=caption,
                voice=MockPtbVoice(payload=payload, mime_type=mime_type),
                date=date or datetime.now(UTC),
            )
        )


class MockPtbBot:
    def __init__(
        self,
        *,
        user_id: int = 1,
        username: str = "coord_bot",
        fail_get_me: Exception | None = None,
        send_message_side_effect: Exception | Callable[..., object] | None = None,
    ) -> None:
        self.user_id = user_id
        self.username = username
        self.sent_messages: list[dict[str, object]] = []
        self._fail_get_me = fail_get_me
        self._send_message_side_effect = send_message_side_effect
        self.get_me = AsyncMock(side_effect=self._get_me)
        self.send_message = AsyncMock(side_effect=self._send_message)

    async def _get_me(self) -> MockPtbMe:
        if self._fail_get_me is not None:
            raise self._fail_get_me
        return MockPtbMe(id=self.user_id, username=self.username)

    async def _send_message(self, **kwargs):
        self.sent_messages.append(dict(kwargs))
        if self._send_message_side_effect is None:
            return None
        if isinstance(self._send_message_side_effect, Exception):
            raise self._send_message_side_effect
        return self._send_message_side_effect(**kwargs)


class MockPtbUpdater:
    def __init__(
        self,
        *,
        fail_polling: Exception | None = None,
    ) -> None:
        self._fail_polling = fail_polling
        self.start_polling = AsyncMock(side_effect=self._start_polling)
        self.stop = AsyncMock(return_value=None)

    async def _start_polling(self) -> None:
        if self._fail_polling is not None:
            raise self._fail_polling


class MockPtbMessageHandler:
    def __init__(self, filters, callback) -> None:
        self.filters = filters
        self.callback = callback


class MockPtbFilter:
    def __or__(self, _other):
        return self


@dataclass(frozen=True)
class MockPtbApplicationSpec:
    bot_user_id: int = 1
    bot_username: str = "coord_bot"
    fail_get_me: Exception | None = None
    fail_initialize: Exception | None = None
    fail_start: Exception | None = None
    fail_polling: Exception | None = None
    send_message_side_effect: Exception | Callable[..., object] | None = None


class MockPtbApplication:
    def __init__(
        self,
        token: str,
        *,
        bot_user_id: int = 1,
        bot_username: str = "coord_bot",
        fail_get_me: Exception | None = None,
        fail_start: bool = False,
        fail_initialize: bool = False,
        fail_polling: bool = False,
        send_message_side_effect: Exception | Callable[..., object] | None = None,
    ) -> None:
        self.token = token
        self.bot = MockPtbBot(
            user_id=bot_user_id,
            username=bot_username,
            fail_get_me=fail_get_me,
            send_message_side_effect=send_message_side_effect,
        )
        self.handlers: list[MockPtbMessageHandler] = []
        initialize_exc = RuntimeError("initialize failed") if fail_initialize else None
        start_exc = RuntimeError("start failed") if fail_start else None
        polling_exc = RuntimeError("polling failed") if fail_polling else None
        self.initialize = AsyncMock(side_effect=self._make_stage(initialize_exc))
        self.start = AsyncMock(side_effect=self._make_stage(start_exc))
        self.stop = AsyncMock(return_value=None)
        self.shutdown = AsyncMock(return_value=None)
        self.updater = MockPtbUpdater(fail_polling=polling_exc)

    @staticmethod
    def _make_stage(exc: Exception | None):
        async def _stage() -> None:
            if exc is not None:
                raise exc

        return _stage

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    async def dispatch_update(
        self,
        update: MockPtbUpdate,
        *,
        context: object | None = None,
    ) -> None:
        resolved_context = (
            context if context is not None else SimpleNamespace(bot=self.bot)
        )
        for handler in self.handlers:
            await handler.callback(update, resolved_context)


class MockPtbApplicationBuilder:
    def __init__(self, runtime: MockPtbRuntime) -> None:
        self._runtime = runtime
        self._token: str | None = None

    def token(self, token: str) -> MockPtbApplicationBuilder:
        self._token = token
        return self

    def build(self) -> MockPtbApplication:
        if self._token is None:
            raise ValueError("mock_ptb_builder_missing_token")
        return self._runtime.build_application(self._token)


class MockPtbRuntime:
    def __init__(
        self,
        app_factory: Callable[[str], MockPtbApplication] | None = None,
        *,
        app_specs_by_token: Mapping[str, MockPtbApplicationSpec] | None = None,
    ) -> None:
        self._app_factory = app_factory
        self._app_specs_by_token = dict(app_specs_by_token or {})
        self.built_tokens: list[str] = []
        self.applications_by_token: dict[str, MockPtbApplication] = {}
        self.filters = SimpleNamespace(
            TEXT=MockPtbFilter(),
            VOICE=MockPtbFilter(),
            PHOTO=MockPtbFilter(),
            CAPTION=MockPtbFilter(),
        )
        self.MessageHandler = MockPtbMessageHandler

    def ApplicationBuilder(self) -> MockPtbApplicationBuilder:
        return MockPtbApplicationBuilder(self)

    def build_application(self, token: str) -> MockPtbApplication:
        self.built_tokens.append(token)
        if self._app_factory is not None:
            application = self._app_factory(token)
        else:
            spec = self._app_specs_by_token.get(token, MockPtbApplicationSpec())
            application = MockPtbApplication(
                token,
                bot_user_id=spec.bot_user_id,
                bot_username=spec.bot_username,
                fail_get_me=spec.fail_get_me,
                fail_initialize=spec.fail_initialize is not None,
                fail_start=spec.fail_start is not None,
                fail_polling=spec.fail_polling is not None,
                send_message_side_effect=spec.send_message_side_effect,
            )
            if spec.fail_initialize is not None:
                application.initialize = AsyncMock(
                    side_effect=MockPtbApplication._make_stage(spec.fail_initialize)
                )
            if spec.fail_start is not None:
                application.start = AsyncMock(
                    side_effect=MockPtbApplication._make_stage(spec.fail_start)
                )
            if spec.fail_polling is not None:
                application.updater = MockPtbUpdater(
                    fail_polling=spec.fail_polling
                )
        self.applications_by_token[token] = application
        return application


class ImmediateExecutorLoop:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    async def run_in_executor(self, _executor, func, *args):
        self.calls.append((func, args))
        return func(*args)
