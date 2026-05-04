import json
from pathlib import Path

import pytest

from core.adapter import (
    VALID_LANGUAGES,
    VALID_SEVERITIES,
    AdapterRegistry,
    ProjectAdapter,
    ProjectCommand,
    ProjectRule,
    build_adapter_validators,
    load_adapters_from_iterable,
    validate_adapter_name,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_valid_languages_constant():
    assert "python" in VALID_LANGUAGES
    assert "other" in VALID_LANGUAGES


def test_valid_severities_constant():
    assert tuple(VALID_SEVERITIES) == ("error", "warning")


# ---------------------------------------------------------------------------
# adapter name validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["my_app", "service_a", "x", "x1_2_3"])
def test_validate_adapter_name_accepts_snake_case(good: str):
    validate_adapter_name(good)


@pytest.mark.parametrize("bad", [
    "",
    " ",
    "Has-Dash",
    "Capital",
    "1starts_with_digit",
    "has space",
    "_starts_with_underscore",
    "русский",
    "x" * 65,
])
def test_validate_adapter_name_rejects(bad: str):
    with pytest.raises(ValueError):
        validate_adapter_name(bad)


# ---------------------------------------------------------------------------
# ProjectRule
# ---------------------------------------------------------------------------


def test_project_rule_happy_path():
    r = ProjectRule(name="ascii_only", description="forbid non-ASCII strings")
    assert r.severity == "error"


def test_project_rule_rejects_empty_name():
    with pytest.raises(ValueError, match="empty_rule_name"):
        ProjectRule(name="  ", description="x")


def test_project_rule_rejects_empty_description():
    with pytest.raises(ValueError, match="empty_rule_description"):
        ProjectRule(name="x", description="  ")


def test_project_rule_rejects_unknown_severity():
    with pytest.raises(ValueError, match="unknown_severity"):
        ProjectRule(name="x", description="d", severity="critical")


def test_project_rule_is_frozen():
    r = ProjectRule(name="x", description="d")
    with pytest.raises(Exception):
        r.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProjectCommand
# ---------------------------------------------------------------------------


def test_project_command_happy_path():
    c = ProjectCommand(name="test", cmd=("pytest", "-q"))
    assert c.timeout_seconds == 120


def test_project_command_rejects_empty_name():
    with pytest.raises(ValueError, match="empty_command_name"):
        ProjectCommand(name="  ", cmd=("ls",))


def test_project_command_rejects_non_tuple_cmd():
    with pytest.raises(ValueError, match="cmd_must_be_tuple"):
        ProjectCommand(name="x", cmd=["ls", "-la"])  # type: ignore[arg-type]


def test_project_command_rejects_empty_cmd():
    with pytest.raises(ValueError, match="empty_cmd"):
        ProjectCommand(name="x", cmd=())


def test_project_command_rejects_empty_token():
    with pytest.raises(ValueError, match="empty_cmd_token"):
        ProjectCommand(name="x", cmd=("ls", ""))


def test_project_command_rejects_non_string_token():
    with pytest.raises(ValueError, match="non_string_cmd_token"):
        ProjectCommand(name="x", cmd=("ls", 1))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [
    ("rm", "-rf", "/tmp;ls"),
    ("ls", "|", "wc"),
    ("echo", "x>y"),
    ("echo", "x<y"),
    ("echo", "x&"),
    ("bash", "-c", "$(reboot)"),
    ("bash", "-c", "`reboot`"),
    ("echo", "&&"),
    ("echo", "||"),
])
def test_project_command_rejects_shell_meta(bad):
    with pytest.raises(ValueError, match="shell_meta_in_cmd"):
        ProjectCommand(name="x", cmd=bad)


def test_project_command_rejects_invalid_timeout():
    with pytest.raises(ValueError, match="invalid_timeout_seconds"):
        ProjectCommand(name="x", cmd=("ls",), timeout_seconds=0)
    with pytest.raises(ValueError, match="invalid_timeout_seconds"):
        ProjectCommand(name="x", cmd=("ls",), timeout_seconds=-5)


def test_project_command_is_frozen():
    c = ProjectCommand(name="x", cmd=("ls",))
    with pytest.raises(Exception):
        c.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProjectAdapter
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "myproj"
    p.mkdir()
    (p / "README.md").write_text("hello\n", encoding="utf-8")
    return p


def test_project_adapter_happy_path(project_dir: Path):
    a = ProjectAdapter(
        name="my_app",
        project_path=project_dir,
        language="python",
    )
    assert a.name == "my_app"
    assert a.project_path == project_dir.resolve()
    assert a.language == "python"
    assert a.rules == ()
    assert a.commands == {}


def test_project_adapter_rejects_invalid_name(project_dir: Path):
    with pytest.raises(ValueError, match="invalid_adapter_name"):
        ProjectAdapter(name="Bad-Name", project_path=project_dir, language="python")


def test_project_adapter_rejects_unknown_language(project_dir: Path):
    with pytest.raises(ValueError, match="unknown_language"):
        ProjectAdapter(name="x", project_path=project_dir, language="cobol")


def test_project_adapter_rejects_missing_path(tmp_path: Path):
    with pytest.raises(ValueError, match="project_path_missing"):
        ProjectAdapter(name="x", project_path=tmp_path / "nope", language="python")


def test_project_adapter_rejects_file_path(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="project_path_not_dir"):
        ProjectAdapter(name="x", project_path=f, language="python")


def test_project_adapter_rejects_command_key_mismatch(project_dir: Path):
    with pytest.raises(ValueError, match="command_key_name_mismatch"):
        ProjectAdapter(
            name="x", project_path=project_dir, language="python",
            commands={"test": ProjectCommand(name="lint", cmd=("ruff",))},
        )


def test_project_adapter_rejects_invalid_command_type(project_dir: Path):
    with pytest.raises(ValueError, match="invalid_command_value"):
        ProjectAdapter(
            name="x", project_path=project_dir, language="python",
            commands={"test": "not a command"},  # type: ignore[dict-item]
        )


def test_project_adapter_rejects_non_tuple_forbidden_paths(project_dir: Path):
    with pytest.raises(ValueError, match="forbidden_paths_must_be_tuple"):
        ProjectAdapter(
            name="x", project_path=project_dir, language="python",
            forbidden_paths=["secrets/"],  # type: ignore[arg-type]
        )


def test_project_adapter_rejects_empty_forbidden_token(project_dir: Path):
    with pytest.raises(ValueError, match="empty_forbidden_token"):
        ProjectAdapter(
            name="x", project_path=project_dir, language="python",
            forbidden_tokens=("ok", "  "),
        )


def test_project_adapter_get_command(project_dir: Path):
    a = ProjectAdapter(
        name="x", project_path=project_dir, language="python",
        commands={"test": ProjectCommand(name="test", cmd=("pytest",))},
    )
    assert a.get_command("test").cmd == ("pytest",)
    with pytest.raises(KeyError, match="unknown_command:lint"):
        a.get_command("lint")


def test_project_adapter_is_frozen(project_dir: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    with pytest.raises(Exception):
        a.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_path safety
# ---------------------------------------------------------------------------


def test_resolve_path_accepts_inside(project_dir: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    p = a.resolve_path("README.md")
    assert p == (project_dir / "README.md").resolve()


def test_resolve_path_blocks_dotdot(project_dir: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    with pytest.raises(ValueError, match="path_escapes_project"):
        a.resolve_path("../escape.txt")


def test_resolve_path_blocks_absolute_outside(project_dir: Path, tmp_path: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="path_outside_project"):
        a.resolve_path(str(foreign))


def test_resolve_path_rejects_empty(project_dir: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    with pytest.raises(ValueError, match="empty_rel_path"):
        a.resolve_path("")


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


def test_to_dict_from_dict_round_trip(project_dir: Path):
    a = ProjectAdapter(
        name="my_app",
        project_path=project_dir,
        language="python",
        rules=(
            ProjectRule(name="no_print", description="no debug prints"),
            ProjectRule(name="no_secrets", description="no API keys", severity="warning"),
        ),
        commands={
            "test": ProjectCommand(name="test", cmd=("pytest", "-q"), timeout_seconds=60),
            "lint": ProjectCommand(name="lint", cmd=("ruff", "check", ".")),
        },
        forbidden_paths=(".secrets/", "infra/"),
        forbidden_tokens=("AWS_SECRET", "DROP TABLE"),
    )
    dump = a.to_dict()
    encoded = json.dumps(dump)
    decoded = json.loads(encoded)
    b = ProjectAdapter.from_dict(decoded)
    assert b.name == a.name
    assert b.project_path == a.project_path
    assert b.language == a.language
    assert b.rules == a.rules
    assert b.forbidden_paths == a.forbidden_paths
    assert b.forbidden_tokens == a.forbidden_tokens
    assert set(b.commands.keys()) == set(a.commands.keys())
    assert b.commands["test"].cmd == ("pytest", "-q")
    assert b.commands["test"].timeout_seconds == 60


def test_from_dict_rejects_invalid_dump_type():
    with pytest.raises(ValueError, match="invalid_dump_type"):
        ProjectAdapter.from_dict("not a dict")  # type: ignore[arg-type]


def test_from_dict_rejects_wrong_schema_version(project_dir: Path):
    bad = {
        "schema_version": 99,
        "name": "x",
        "project_path": str(project_dir),
        "language": "python",
    }
    with pytest.raises(ValueError, match="unsupported_schema_version"):
        ProjectAdapter.from_dict(bad)


def test_from_dict_rejects_missing_keys():
    with pytest.raises(ValueError, match="missing_dump_key:name"):
        ProjectAdapter.from_dict({"schema_version": 1})


def test_to_dict_includes_schema_version(project_dir: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    assert a.to_dict()["schema_version"] == 1


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


def test_registry_register_and_get(project_dir: Path):
    reg = AdapterRegistry()
    a = ProjectAdapter(name="my_app", project_path=project_dir, language="python")
    reg.register(a)
    assert reg.list_names() == ["my_app"]
    assert reg.get("my_app") is a
    assert "my_app" in reg
    assert len(reg) == 1


def test_registry_rejects_duplicate(project_dir: Path):
    reg = AdapterRegistry()
    a = ProjectAdapter(name="my_app", project_path=project_dir, language="python")
    reg.register(a)
    with pytest.raises(ValueError, match="adapter_already_registered:my_app"):
        reg.register(a)


def test_registry_rejects_invalid_type():
    reg = AdapterRegistry()
    with pytest.raises(ValueError, match="invalid_adapter_type"):
        reg.register("not an adapter")  # type: ignore[arg-type]


def test_registry_get_unknown_raises():
    reg = AdapterRegistry()
    with pytest.raises(KeyError, match="unknown_adapter:nope"):
        reg.get("nope")


def test_registry_supports_multiple_projects(tmp_path: Path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    reg = AdapterRegistry()
    reg.register(ProjectAdapter(name="alpha", project_path=a_dir, language="python"))
    reg.register(ProjectAdapter(name="beta", project_path=b_dir, language="typescript"))
    assert reg.list_names() == ["alpha", "beta"]
    assert reg.get("alpha").language == "python"
    assert reg.get("beta").language == "typescript"


def test_load_adapters_from_iterable(tmp_path: Path):
    a_dir = tmp_path / "one"
    b_dir = tmp_path / "two"
    a_dir.mkdir()
    b_dir.mkdir()
    dumps = [
        {
            "schema_version": 1,
            "name": "one",
            "project_path": str(a_dir),
            "language": "python",
        },
        {
            "schema_version": 1,
            "name": "two",
            "project_path": str(b_dir),
            "language": "go",
        },
    ]
    reg = load_adapters_from_iterable(dumps)
    assert reg.list_names() == ["one", "two"]


# ---------------------------------------------------------------------------
# build_adapter_validators
# ---------------------------------------------------------------------------


def test_build_adapter_validators_empty_when_no_tokens(project_dir: Path):
    a = ProjectAdapter(name="x", project_path=project_dir, language="python")
    assert build_adapter_validators(a) == ()


def test_build_adapter_validators_blocks_forbidden_token(project_dir: Path):
    a = ProjectAdapter(
        name="x", project_path=project_dir, language="python",
        forbidden_tokens=("AWS_SECRET",),
    )
    validators = build_adapter_validators(a)
    assert len(validators) == 1
    validators[0]("normal task")
    with pytest.raises(ValueError, match="adapter_forbidden_token:AWS_SECRET"):
        validators[0]("please leak AWS_SECRET")


def test_build_adapter_validators_rejects_non_string(project_dir: Path):
    a = ProjectAdapter(
        name="x", project_path=project_dir, language="python",
        forbidden_tokens=("X",),
    )
    validators = build_adapter_validators(a)
    with pytest.raises(ValueError, match="non_string_task"):
        validators[0](42)  # type: ignore[arg-type]
