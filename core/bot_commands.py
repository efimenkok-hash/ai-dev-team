"""
core/bot_commands.py

Step 14a: parser + registry for slash-commands. The Telegram bridge calls
parse_command() on every incoming message; if it returns a BotCommand,
the bridge dispatches via CommandRegistry. The actual handlers live in
the bridge (they need runtime state — orchestrator, adapter registry,
observability — that doesn't belong in this pure-logic module).

CONTRACTS:
1. CommandName enum lists exactly the 9 commands the spec requires.
2. parse_command(text) is total: it never raises. Returns BotCommand or
   None. Recognises both '/cmd args' and '/cmd@botname args' (Telegram
   appends '@botname' in group chats).
3. BotCommand is frozen; args is a tuple of trimmed non-empty strings;
   raw_text is the original input (after .strip()).
4. CommandRegistry rejects duplicate registrations and unknown dispatches.
5. dispatch() never swallows handler errors — propagates so the bridge can
   log them; but it does enforce string return type (no None / non-str).
6. format_help_text() is deterministic in command order (matches enum).
7. parse_budget_amount accepts '5', '5.50', '5$', '$5', '5 USD', '5usd',
   case-insensitive; returns float USD; rejects negative / non-numeric;
   returns None if args is empty.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class CommandName(str, Enum):
    PROJECT = "project"
    PROJECTS = "projects"
    SWITCH = "switch"
    BUDGET = "budget"
    AGENTS = "agents"
    TIER = "tier"
    LOG = "log"
    STOP = "stop"
    RETRY = "retry"
    PUSH = "push"
    PR = "pr"
    HELP = "help"


# Russian-language descriptions; these match what we registered in BotFather
# and what /help renders.
COMMAND_DESCRIPTIONS: dict[CommandName, str] = {
    CommandName.PROJECT: "текущий project context (/project)",
    CommandName.PROJECTS: (
        "проекты и binding чата "
        "(/projects [here|bind|migrate here|unbind])"
    ),
    CommandName.SWITCH: "статус project context (/switch [project])",
    CommandName.BUDGET: "бюджет (/budget [сумма])",
    CommandName.AGENTS: "состав и перфоманс команды",
    CommandName.TIER: "тариф моделей (/tier [set <имя>|reset])",
    CommandName.LOG: "лог последней задачи",
    CommandName.STOP: "остановить текущую задачу",
    CommandName.RETRY: "повторить (/retry [--different])",
    CommandName.PUSH: "запушить ветку в GitHub (/push <task_id>)",
    CommandName.PR: "создать draft PR (/pr <task_id>)",
    CommandName.HELP: "эта справка",
}

# Thematic emojis for /help rendering. Decoupled from descriptions so
# BotFather command list (which doesn't allow emoji prefixes) stays clean.
COMMAND_EMOJIS: dict[CommandName, str] = {
    CommandName.PROJECT: "📌",
    CommandName.PROJECTS: "📋",
    CommandName.SWITCH: "🔄",
    CommandName.BUDGET: "💰",
    CommandName.AGENTS: "👥",
    CommandName.TIER: "💼",
    CommandName.LOG: "📜",
    CommandName.STOP: "⏹",
    CommandName.RETRY: "🔁",
    CommandName.PUSH: "🚀",
    CommandName.PR: "🪄",
    CommandName.HELP: "❓",
}


@dataclass(frozen=True)
class BotCommand:
    name: CommandName
    args: tuple[str, ...]
    raw_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, CommandName):
            raise ValueError(f"invalid_command_name:{self.name!r}")
        if not isinstance(self.args, tuple):
            raise ValueError("args_must_be_tuple")
        for arg in self.args:
            if not isinstance(arg, str) or not arg.strip():
                raise ValueError("empty_arg")
        if not isinstance(self.raw_text, str) or not self.raw_text.strip():
            raise ValueError("empty_raw_text")

    def has_flag(self, flag: str) -> bool:
        """Convenience: does the args list contain '--<flag>'?"""
        if not flag:
            return False
        target = f"--{flag}" if not flag.startswith("--") else flag
        return target in self.args

    def positional_args(self) -> tuple[str, ...]:
        """Args minus any --flag-style options."""
        return tuple(a for a in self.args if not a.startswith("--"))


def parse_command(text: str) -> BotCommand | None:
    """Parse a Telegram message into a BotCommand if it's a known slash-command.

    Returns None if:
      - text is not a string
      - text doesn't start with '/'
      - the command head doesn't match a known CommandName
    Never raises.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]  # strip leading '/'
    if not body:
        return None
    head, _, tail = body.partition(" ")
    # Telegram appends '@botname' in group chats: '/help@my_bot' -> 'help'
    head = head.split("@", 1)[0].strip().lower()
    if not head:
        return None
    try:
        name = CommandName(head)
    except ValueError:
        return None
    args = tuple(a for a in tail.split() if a)
    return BotCommand(name=name, args=args, raw_text=stripped)


# Handler signature: (BotCommand, ctx) -> reply_text. Context is opaque
# to this module — bridge passes whatever its handlers need.
CommandHandler = Callable[[BotCommand, Any], str]


class CommandRegistry:
    """Maps CommandName -> handler. Bridge populates with closures over
    its runtime state.
    """

    def __init__(self) -> None:
        self._handlers: dict[CommandName, CommandHandler] = {}

    def register(self, name: CommandName, handler: CommandHandler) -> None:
        if not isinstance(name, CommandName):
            raise ValueError(f"invalid_command_name:{name!r}")
        if not callable(handler):
            raise ValueError(f"handler_not_callable:{type(handler).__name__}")
        if name in self._handlers:
            raise ValueError(f"duplicate_handler:{name.value}")
        self._handlers[name] = handler

    def dispatch(self, cmd: BotCommand, ctx: Any = None) -> str:
        if not isinstance(cmd, BotCommand):
            raise ValueError(f"invalid_command_type:{type(cmd).__name__}")
        if cmd.name not in self._handlers:
            raise KeyError(f"no_handler_for:{cmd.name.value}")
        result = self._handlers[cmd.name](cmd, ctx)
        if not isinstance(result, str):
            raise TypeError(
                f"handler_returned_non_string:{type(result).__name__}"
            )
        return result

    def list_registered(self) -> tuple[CommandName, ...]:
        """Stable iteration order: CommandName enum order, filtered by
        what's actually registered.
        """
        return tuple(n for n in CommandName if n in self._handlers)

    def is_registered(self, name: CommandName) -> bool:
        return name in self._handlers

    def __contains__(self, name: object) -> bool:
        return name in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)


def format_help_text(
    registered: tuple[CommandName, ...] = tuple(CommandName),
    *,
    header: str = "🛠 Доступные команды",
) -> str:
    """Renders a Russian help block listing the registered commands.

    Output:
        🛠 Доступные команды

        📋 /projects — список проектов
        🔄 /switch — переключить проект (/switch <имя>)
        ...
    """
    if not isinstance(registered, tuple):
        raise ValueError("registered_must_be_tuple")
    if not isinstance(header, str) or not header.strip():
        raise ValueError("empty_header")
    lines: list[str] = [header, ""]
    for cmd in registered:
        if not isinstance(cmd, CommandName):
            raise ValueError(f"invalid_command_in_list:{cmd!r}")
        emoji = COMMAND_EMOJIS.get(cmd, "•")
        desc = COMMAND_DESCRIPTIONS.get(cmd, "")
        lines.append(f"{emoji} /{cmd.value} — {desc}")
    return "\n".join(lines)


def parse_budget_amount(args: tuple[str, ...]) -> float | None:
    """Parse '/budget 5', '/budget 5.50', '/budget 5$', '/budget $5',
    '/budget 5 USD', '/budget 5usd' into a float (USD).

    Returns:
      - float if a valid amount can be extracted
      - None if args is empty (caller treats as "show, don't set")

    Raises:
      ValueError on negative, NaN, or non-numeric content.
    """
    if not isinstance(args, tuple):
        raise ValueError("args_must_be_tuple")
    if not args:
        return None
    # Concatenate args and strip currency markers.
    raw = "".join(args).strip().lower()
    if not raw:
        return None
    # Strip currency markers in any combination. Order matters: longer
    # markers must come first so "долларов" doesn't leave "ов" behind after
    # stripping "доллар".
    for marker in ("долларов", "доллара", "доллар", "usd", "$"):
        raw = raw.replace(marker, "")
    raw = raw.strip()
    if not raw:
        raise ValueError("empty_amount_after_strip")
    try:
        amount = float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid_amount:{raw}") from exc
    if amount != amount:  # NaN check
        raise ValueError("nan_amount")
    if amount < 0:
        raise ValueError(f"negative_amount:{amount}")
    return amount
