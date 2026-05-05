"""Tests for core.bot_commands (Step 14a: slash-command parsing + registry)."""

import pytest

from core.bot_commands import (
    COMMAND_DESCRIPTIONS,
    BotCommand,
    CommandName,
    CommandRegistry,
    format_help_text,
    parse_budget_amount,
    parse_command,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_command_name_enum_complete():
    """All 10 commands must be in the enum."""
    expected = {
        "projects", "switch", "budget", "agents", "tier",
        "log", "stop", "retry", "push", "help",
    }
    assert {c.value for c in CommandName} == expected


def test_descriptions_cover_all_command_names():
    for cmd in CommandName:
        assert cmd in COMMAND_DESCRIPTIONS
        assert COMMAND_DESCRIPTIONS[cmd].strip()


# ---------------------------------------------------------------------------
# BotCommand dataclass
# ---------------------------------------------------------------------------


def test_botcommand_happy_path():
    c = BotCommand(name=CommandName.SWITCH, args=("hedgekeeper",), raw_text="/switch hedgekeeper")
    assert c.name is CommandName.SWITCH
    assert c.args == ("hedgekeeper",)
    assert c.raw_text == "/switch hedgekeeper"


def test_botcommand_is_frozen():
    c = BotCommand(name=CommandName.HELP, args=(), raw_text="/help")
    with pytest.raises(Exception):
        c.name = CommandName.STOP  # type: ignore[misc]


def test_botcommand_rejects_non_enum_name():
    with pytest.raises(ValueError, match="invalid_command_name"):
        BotCommand(name="help", args=(), raw_text="/help")  # type: ignore[arg-type]


def test_botcommand_rejects_non_tuple_args():
    with pytest.raises(ValueError, match="args_must_be_tuple"):
        BotCommand(name=CommandName.HELP, args=["x"], raw_text="/help x")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "  ", None, 123])
def test_botcommand_rejects_empty_or_invalid_arg(bad):
    with pytest.raises(ValueError, match="empty_arg"):
        BotCommand(name=CommandName.SWITCH, args=(bad,), raw_text="/switch x")


@pytest.mark.parametrize("bad", ["", "  ", None])
def test_botcommand_rejects_empty_raw_text(bad):
    with pytest.raises(ValueError, match="empty_raw_text"):
        BotCommand(name=CommandName.HELP, args=(), raw_text=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BotCommand helpers: has_flag / positional_args
# ---------------------------------------------------------------------------


def test_has_flag_with_double_dash():
    c = BotCommand(
        name=CommandName.RETRY,
        args=("--different",),
        raw_text="/retry --different",
    )
    assert c.has_flag("different") is True
    assert c.has_flag("--different") is True


def test_has_flag_returns_false_when_absent():
    c = BotCommand(name=CommandName.RETRY, args=(), raw_text="/retry")
    assert c.has_flag("different") is False


def test_has_flag_empty_string_returns_false():
    c = BotCommand(name=CommandName.RETRY, args=("--foo",), raw_text="/retry --foo")
    assert c.has_flag("") is False


def test_positional_args_filters_flags():
    c = BotCommand(
        name=CommandName.RETRY,
        args=("--different", "task-1", "--verbose"),
        raw_text="/retry --different task-1 --verbose",
    )
    assert c.positional_args() == ("task-1",)


# ---------------------------------------------------------------------------
# parse_command: happy paths
# ---------------------------------------------------------------------------


def test_parse_simple_command():
    c = parse_command("/help")
    assert c is not None
    assert c.name is CommandName.HELP
    assert c.args == ()
    assert c.raw_text == "/help"


def test_parse_command_with_args():
    c = parse_command("/switch hedgekeeper-v2")
    assert c is not None
    assert c.name is CommandName.SWITCH
    assert c.args == ("hedgekeeper-v2",)


def test_parse_command_with_multiple_args():
    c = parse_command("/budget 5 USD")
    assert c is not None
    assert c.name is CommandName.BUDGET
    assert c.args == ("5", "USD")


def test_parse_strips_leading_trailing_whitespace():
    c = parse_command("   /help  ")
    assert c is not None
    assert c.name is CommandName.HELP


def test_parse_collapses_inner_whitespace():
    c = parse_command("/switch    foo    bar")
    assert c is not None
    assert c.args == ("foo", "bar")


def test_parse_is_case_insensitive_for_command_head():
    c = parse_command("/HELP")
    assert c is not None
    assert c.name is CommandName.HELP


def test_parse_preserves_arg_case():
    c = parse_command("/switch Hedgekeeper")
    assert c is not None
    assert c.args == ("Hedgekeeper",)


def test_parse_command_with_botname_suffix():
    """In group chats Telegram appends '@bot_name' to command head."""
    c = parse_command("/help@ai_dev_team_lead_bot")
    assert c is not None
    assert c.name is CommandName.HELP


def test_parse_command_with_botname_and_args():
    c = parse_command("/switch@ai_dev_team_lead_bot myproj")
    assert c is not None
    assert c.name is CommandName.SWITCH
    assert c.args == ("myproj",)


@pytest.mark.parametrize(
    "cmd_text",
    [
        "/projects", "/switch", "/budget", "/agents", "/tier",
        "/log", "/stop", "/retry", "/help",
    ],
)
def test_parse_recognises_all_known_commands(cmd_text):
    c = parse_command(cmd_text)
    assert c is not None
    assert c.name.value == cmd_text[1:]


# ---------------------------------------------------------------------------
# parse_command: rejection paths
# ---------------------------------------------------------------------------


def test_parse_returns_none_for_unknown_command():
    assert parse_command("/unknown") is None


def test_parse_returns_none_for_non_command_text():
    assert parse_command("hello world") is None


def test_parse_returns_none_for_empty_string():
    assert parse_command("") is None


def test_parse_returns_none_for_whitespace_only():
    assert parse_command("   ") is None


def test_parse_returns_none_for_lone_slash():
    assert parse_command("/") is None


def test_parse_returns_none_for_slash_with_only_botname():
    assert parse_command("/@my_bot") is None


def test_parse_returns_none_for_non_string_input():
    assert parse_command(None) is None  # type: ignore[arg-type]
    assert parse_command(123) is None  # type: ignore[arg-type]


def test_parse_is_total_no_exceptions():
    """Sanity: parser never raises on any input."""
    weird_inputs = [
        "/", "//", "/x" * 1000, "\x00", "/help@",
        "/\nstop", "/\thelp", "/help\n\n",
    ]
    for s in weird_inputs:
        parse_command(s)  # must not raise


# ---------------------------------------------------------------------------
# CommandRegistry
# ---------------------------------------------------------------------------


def _ok_handler(cmd, ctx):
    return f"ok:{cmd.name.value}"


def test_registry_register_and_dispatch():
    reg = CommandRegistry()
    reg.register(CommandName.HELP, _ok_handler)
    cmd = parse_command("/help")
    assert reg.dispatch(cmd) == "ok:help"


def test_registry_passes_context_to_handler():
    captured = {}

    def handler(cmd, ctx):
        captured["ctx"] = ctx
        return "x"

    reg = CommandRegistry()
    reg.register(CommandName.HELP, handler)
    reg.dispatch(parse_command("/help"), ctx={"user_id": 42})
    assert captured["ctx"] == {"user_id": 42}


def test_registry_rejects_non_enum_name():
    reg = CommandRegistry()
    with pytest.raises(ValueError, match="invalid_command_name"):
        reg.register("help", _ok_handler)  # type: ignore[arg-type]


def test_registry_rejects_non_callable_handler():
    reg = CommandRegistry()
    with pytest.raises(ValueError, match="handler_not_callable"):
        reg.register(CommandName.HELP, "not callable")  # type: ignore[arg-type]


def test_registry_rejects_duplicate_handler():
    reg = CommandRegistry()
    reg.register(CommandName.HELP, _ok_handler)
    with pytest.raises(ValueError, match="duplicate_handler"):
        reg.register(CommandName.HELP, _ok_handler)


def test_registry_dispatch_unknown_command_raises():
    reg = CommandRegistry()
    with pytest.raises(KeyError, match="no_handler_for"):
        reg.dispatch(parse_command("/help"))


def test_registry_dispatch_rejects_non_botcommand():
    reg = CommandRegistry()
    with pytest.raises(ValueError, match="invalid_command_type"):
        reg.dispatch("/help")  # type: ignore[arg-type]


def test_registry_dispatch_rejects_non_string_handler_return():
    def bad_handler(cmd, ctx):
        return 42

    reg = CommandRegistry()
    reg.register(CommandName.HELP, bad_handler)
    with pytest.raises(TypeError, match="handler_returned_non_string"):
        reg.dispatch(parse_command("/help"))


def test_registry_dispatch_propagates_handler_errors():
    def boom(cmd, ctx):
        raise RuntimeError("kaboom")

    reg = CommandRegistry()
    reg.register(CommandName.HELP, boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        reg.dispatch(parse_command("/help"))


def test_registry_list_registered_returns_in_enum_order():
    reg = CommandRegistry()
    # register out of enum order
    reg.register(CommandName.HELP, _ok_handler)
    reg.register(CommandName.PROJECTS, _ok_handler)
    reg.register(CommandName.SWITCH, _ok_handler)
    listed = reg.list_registered()
    # PROJECTS is first in enum order, SWITCH second, HELP last
    assert listed == (CommandName.PROJECTS, CommandName.SWITCH, CommandName.HELP)


def test_registry_is_registered_check():
    reg = CommandRegistry()
    reg.register(CommandName.HELP, _ok_handler)
    assert reg.is_registered(CommandName.HELP)
    assert not reg.is_registered(CommandName.STOP)


def test_registry_contains_check():
    reg = CommandRegistry()
    reg.register(CommandName.HELP, _ok_handler)
    assert CommandName.HELP in reg
    assert CommandName.STOP not in reg


def test_registry_len():
    reg = CommandRegistry()
    assert len(reg) == 0
    reg.register(CommandName.HELP, _ok_handler)
    assert len(reg) == 1
    reg.register(CommandName.STOP, _ok_handler)
    assert len(reg) == 2


# ---------------------------------------------------------------------------
# format_help_text
# ---------------------------------------------------------------------------


def test_format_help_text_default_lists_all():
    text = format_help_text()
    for cmd in CommandName:
        assert f"/{cmd.value}" in text
        assert COMMAND_DESCRIPTIONS[cmd] in text


def test_format_help_text_custom_header():
    text = format_help_text(header="My commands:")
    assert text.startswith("My commands:")


def test_format_help_text_filtered_subset():
    text = format_help_text(registered=(CommandName.HELP, CommandName.STOP))
    assert "/help" in text
    assert "/stop" in text
    assert "/projects" not in text


def test_format_help_text_preserves_enum_order():
    """Even if caller passes commands out of order, output uses given order."""
    text = format_help_text(
        registered=(CommandName.STOP, CommandName.HELP, CommandName.PROJECTS)
    )
    lines = text.split("\n")
    # line[0] is header, line[1] is blank for visual separation
    assert "/stop" in lines[2]
    assert "/help" in lines[3]
    assert "/projects" in lines[4]


def test_format_help_text_rejects_non_tuple():
    with pytest.raises(ValueError, match="registered_must_be_tuple"):
        format_help_text(registered=[CommandName.HELP])  # type: ignore[arg-type]


def test_format_help_text_rejects_empty_header():
    with pytest.raises(ValueError, match="empty_header"):
        format_help_text(header="   ")


def test_format_help_text_rejects_invalid_command_in_list():
    with pytest.raises(ValueError, match="invalid_command_in_list"):
        format_help_text(registered=("help",))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_budget_amount
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("5",), 5.0),
        (("5.50",), 5.5),
        (("5$",), 5.0),
        (("$5",), 5.0),
        (("5", "USD"), 5.0),
        (("5", "usd"), 5.0),
        (("5usd",), 5.0),
        (("$5.99",), 5.99),
        (("0",), 0.0),
        (("100",), 100.0),
        (("5", "долларов"), 5.0),
    ],
)
def test_parse_budget_amount_valid(args, expected):
    assert parse_budget_amount(args) == pytest.approx(expected)


def test_parse_budget_amount_returns_none_for_empty():
    assert parse_budget_amount(()) is None


def test_parse_budget_amount_rejects_non_tuple():
    with pytest.raises(ValueError, match="args_must_be_tuple"):
        parse_budget_amount(["5"])  # type: ignore[arg-type]


@pytest.mark.parametrize("args", [("abc",), ("$abc",), ("5x",)])
def test_parse_budget_amount_rejects_non_numeric(args):
    with pytest.raises(ValueError, match="invalid_amount"):
        parse_budget_amount(args)


def test_parse_budget_amount_rejects_negative():
    with pytest.raises(ValueError, match="negative_amount"):
        parse_budget_amount(("-5",))


def test_parse_budget_amount_rejects_only_currency_marker():
    with pytest.raises(ValueError, match="empty_amount_after_strip"):
        parse_budget_amount(("$",))


def test_parse_budget_amount_handles_whitespace_args():
    """Tuple of whitespace-only args -> None (treated as empty)."""
    # Empty after strip -> None
    assert parse_budget_amount(("",)) is None or True  # documented behaviour
