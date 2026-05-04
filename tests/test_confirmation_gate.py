"""Tests for core.confirmation_gate (Step 14a: auto-vs-ask policy)."""

import pytest

from core.confirmation_gate import (
    DEFAULT_COST_THRESHOLD_USD,
    DEFAULT_PROTECTED_PATHS,
    ActionDescriptor,
    ActionKind,
    BatchDecision,
    ConfirmationDecision,
    ConfirmationGate,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_action_kind_enum_values():
    assert ActionKind.WRITE_NEW.value == "WRITE_NEW"
    assert ActionKind.MODIFY.value == "MODIFY"
    assert ActionKind.DELETE.value == "DELETE"
    assert ActionKind.RENAME.value == "RENAME"
    assert ActionKind.ADD_DEPENDENCY.value == "ADD_DEPENDENCY"
    assert ActionKind.REMOVE_DEPENDENCY.value == "REMOVE_DEPENDENCY"
    assert ActionKind.CI_CHANGE.value == "CI_CHANGE"
    assert ActionKind.PUSH_TO_MAIN.value == "PUSH_TO_MAIN"


def test_default_protected_paths_contains_core_modules():
    assert "core/orchestrator.py" in DEFAULT_PROTECTED_PATHS
    assert "core/fsm.py" in DEFAULT_PROTECTED_PATHS
    assert "core/runtime_validator.py" in DEFAULT_PROTECTED_PATHS
    assert ".github/workflows/" in DEFAULT_PROTECTED_PATHS
    assert "pyproject.toml" in DEFAULT_PROTECTED_PATHS


def test_default_cost_threshold_is_one_dollar():
    assert DEFAULT_COST_THRESHOLD_USD == 1.0


# ---------------------------------------------------------------------------
# ActionDescriptor construction
# ---------------------------------------------------------------------------


def test_action_descriptor_happy_path():
    a = ActionDescriptor(
        kind=ActionKind.MODIFY,
        target_path="core/billing.py",
        detail="add VAT calculation",
        estimated_cost_usd=0.15,
    )
    assert a.kind is ActionKind.MODIFY
    assert a.target_path == "core/billing.py"
    assert a.detail == "add VAT calculation"
    assert a.estimated_cost_usd == 0.15


def test_action_descriptor_defaults():
    a = ActionDescriptor(kind=ActionKind.PUSH_TO_MAIN)
    assert a.target_path == ""
    assert a.detail == ""
    assert a.estimated_cost_usd == 0.0


def test_action_descriptor_is_frozen():
    a = ActionDescriptor(kind=ActionKind.MODIFY)
    with pytest.raises(Exception):
        a.target_path = "x"  # type: ignore[misc]


def test_action_descriptor_rejects_non_enum_kind():
    with pytest.raises(ValueError, match="invalid_kind"):
        ActionDescriptor(kind="MODIFY")  # type: ignore[arg-type]


def test_action_descriptor_rejects_non_string_target_path():
    with pytest.raises(ValueError, match="non_string_target_path"):
        ActionDescriptor(kind=ActionKind.MODIFY, target_path=123)  # type: ignore[arg-type]


def test_action_descriptor_rejects_non_string_detail():
    with pytest.raises(ValueError, match="non_string_detail"):
        ActionDescriptor(kind=ActionKind.MODIFY, detail=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_path",
    ["../etc/passwd", "core/../secrets", "../../up", "a/b/../c"],
)
def test_action_descriptor_rejects_path_traversal(bad_path):
    with pytest.raises(ValueError, match="path_traversal"):
        ActionDescriptor(kind=ActionKind.MODIFY, target_path=bad_path)


def test_action_descriptor_accepts_dot_dot_inside_filename():
    """File named '..config' is fine; only path component '..' is rejected."""
    ActionDescriptor(kind=ActionKind.MODIFY, target_path="core/..config")


@pytest.mark.parametrize("bad", [-0.01, -1, -100])
def test_action_descriptor_rejects_negative_cost(bad):
    with pytest.raises(ValueError, match="invalid_cost"):
        ActionDescriptor(kind=ActionKind.MODIFY, estimated_cost_usd=bad)


def test_action_descriptor_rejects_bool_cost():
    with pytest.raises(ValueError, match="invalid_cost"):
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            estimated_cost_usd=True,  # type: ignore[arg-type]
        )


def test_action_descriptor_rejects_non_numeric_cost():
    with pytest.raises(ValueError, match="invalid_cost"):
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            estimated_cost_usd="0.5",  # type: ignore[arg-type]
        )


def test_action_descriptor_accepts_zero_cost():
    a = ActionDescriptor(kind=ActionKind.MODIFY, estimated_cost_usd=0.0)
    assert a.estimated_cost_usd == 0.0


# ---------------------------------------------------------------------------
# ConfirmationGate construction
# ---------------------------------------------------------------------------


def test_gate_default_construction():
    g = ConfirmationGate()
    assert g.cost_threshold_usd == DEFAULT_COST_THRESHOLD_USD
    assert "core/orchestrator.py" in g.protected_paths


def test_gate_custom_protected_paths():
    g = ConfirmationGate(protected_paths=("custom/file.py",))
    assert g.protected_paths == ("custom/file.py",)


def test_gate_custom_cost_threshold():
    g = ConfirmationGate(cost_threshold_usd=5.0)
    assert g.cost_threshold_usd == 5.0


def test_gate_rejects_non_tuple_protected_paths():
    with pytest.raises(ValueError, match="protected_paths_must_be_tuple"):
        ConfirmationGate(protected_paths=["x"])  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "  ", "\n"])
def test_gate_rejects_empty_protected_path(bad):
    with pytest.raises(ValueError, match="empty_protected_path"):
        ConfirmationGate(protected_paths=(bad,))


def test_gate_rejects_non_string_protected_path():
    with pytest.raises(ValueError, match="empty_protected_path"):
        ConfirmationGate(protected_paths=(123,))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-0.01, -1])
def test_gate_rejects_negative_cost_threshold(bad):
    with pytest.raises(ValueError, match="invalid_cost_threshold"):
        ConfirmationGate(cost_threshold_usd=bad)


def test_gate_rejects_bool_cost_threshold():
    with pytest.raises(ValueError, match="invalid_cost_threshold"):
        ConfirmationGate(cost_threshold_usd=True)  # type: ignore[arg-type]


def test_gate_rejects_non_numeric_cost_threshold():
    with pytest.raises(ValueError, match="invalid_cost_threshold"):
        ConfirmationGate(cost_threshold_usd="1.0")  # type: ignore[arg-type]


def test_gate_normalises_protected_paths():
    g = ConfirmationGate(protected_paths=("./foo.py", "  ./bar/  "))
    assert g.protected_paths == ("foo.py", "bar/")


# ---------------------------------------------------------------------------
# evaluate() — high cost
# ---------------------------------------------------------------------------


def test_high_cost_triggers_confirmation_regardless_of_kind():
    gate = ConfirmationGate(cost_threshold_usd=1.0)
    a = ActionDescriptor(
        kind=ActionKind.MODIFY,
        target_path="some/random/file.py",
        estimated_cost_usd=2.5,
    )
    d = gate.evaluate(a)
    assert d.require_confirmation is True
    assert "$2.50" in d.reason
    assert "$1.00" in d.reason
    assert "стоимость" in d.reason


def test_cost_at_threshold_does_not_trigger():
    gate = ConfirmationGate(cost_threshold_usd=1.0)
    a = ActionDescriptor(
        kind=ActionKind.MODIFY,
        target_path="random/file.py",
        estimated_cost_usd=1.0,
    )
    d = gate.evaluate(a)
    assert d.require_confirmation is False


def test_cost_zero_does_not_trigger():
    gate = ConfirmationGate(cost_threshold_usd=1.0)
    a = ActionDescriptor(
        kind=ActionKind.MODIFY,
        target_path="random/file.py",
        estimated_cost_usd=0.0,
    )
    d = gate.evaluate(a)
    assert d.require_confirmation is False


# ---------------------------------------------------------------------------
# evaluate() — always-ask kinds
# ---------------------------------------------------------------------------


def test_delete_always_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(kind=ActionKind.DELETE, target_path="random/file.py")
    )
    assert d.require_confirmation is True
    assert "удаление" in d.reason
    assert "random/file.py" in d.reason


def test_rename_always_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.RENAME,
            target_path="x.py",
            detail="x.py -> y.py",
        )
    )
    assert d.require_confirmation is True
    assert "переименование" in d.reason
    assert "x.py -> y.py" in d.reason


def test_add_dependency_always_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.ADD_DEPENDENCY,
            detail="numpy>=2.0",
        )
    )
    assert d.require_confirmation is True
    assert "новой зависимости" in d.reason
    assert "numpy>=2.0" in d.reason


def test_remove_dependency_always_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.REMOVE_DEPENDENCY,
            detail="legacy-pkg",
        )
    )
    assert d.require_confirmation is True
    assert "удаление зависимости" in d.reason


def test_ci_change_always_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.CI_CHANGE,
            target_path=".github/workflows/ci.yml",
        )
    )
    assert d.require_confirmation is True
    assert "CI" in d.reason


def test_push_to_main_always_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.PUSH_TO_MAIN,
            detail="initial commit",
        )
    )
    assert d.require_confirmation is True
    assert "push" in d.reason
    assert "main" in d.reason


# ---------------------------------------------------------------------------
# evaluate() — protected paths
# ---------------------------------------------------------------------------


def test_modify_protected_file_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="core/orchestrator.py",
            detail="add new state",
        )
    )
    assert d.require_confirmation is True
    assert "core/orchestrator.py" in d.reason
    assert "критическая" in d.reason or "ядро" in d.reason


def test_modify_file_under_protected_dir_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path=".github/workflows/ci.yml",
        )
    )
    assert d.require_confirmation is True
    assert ".github/workflows/" in d.reason


def test_modify_file_outside_protected_does_not_ask():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="core/billing.py",  # not in DEFAULT_PROTECTED_PATHS
        )
    )
    assert d.require_confirmation is False


def test_write_new_file_in_protected_dir_asks():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.WRITE_NEW,
            target_path="scripts/deploy.sh",
        )
    )
    assert d.require_confirmation is True


def test_write_new_file_outside_protected_does_not_ask():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.WRITE_NEW,
            target_path="docs/blog/post1.md",
        )
    )
    assert d.require_confirmation is False


def test_no_false_positive_on_substring_match():
    """'corex/file.py' must NOT match protected 'core/'."""
    gate = ConfirmationGate(protected_paths=("core/",))
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="corex/file.py",
        )
    )
    assert d.require_confirmation is False


def test_exact_protected_file_match():
    gate = ConfirmationGate(protected_paths=("config.yaml",))
    d = gate.evaluate(
        ActionDescriptor(kind=ActionKind.MODIFY, target_path="config.yaml")
    )
    assert d.require_confirmation is True


def test_path_normalisation_strips_leading_dot_slash():
    gate = ConfirmationGate(protected_paths=("core/",))
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="./core/billing.py",
        )
    )
    assert d.require_confirmation is True


def test_modify_with_empty_target_path_does_not_ask_via_protected():
    """Without a path, protected-path check can't fire."""
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(kind=ActionKind.MODIFY, target_path="")
    )
    assert d.require_confirmation is False


# ---------------------------------------------------------------------------
# evaluate() — auto path
# ---------------------------------------------------------------------------


def test_modify_unprotected_file_auto():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="some/normal/file.py",
            estimated_cost_usd=0.05,
        )
    )
    assert d.require_confirmation is False
    assert d.reason == "auto: безопасное действие в рамках политики"


def test_write_new_unprotected_file_auto():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.WRITE_NEW,
            target_path="docs/post.md",
        )
    )
    assert d.require_confirmation is False


# ---------------------------------------------------------------------------
# evaluate() — order of checks (cost > kind > protected > auto)
# ---------------------------------------------------------------------------


def test_high_cost_wins_over_protected_path_check():
    """Both triggers, but cost is the more fundamental reason."""
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="core/orchestrator.py",
            estimated_cost_usd=5.0,
        )
    )
    assert d.require_confirmation is True
    assert "$5.00" in d.reason  # cost reason wins


def test_high_cost_wins_over_always_ask_kind():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.DELETE,
            target_path="some.py",
            estimated_cost_usd=5.0,
        )
    )
    assert d.require_confirmation is True
    assert "$5.00" in d.reason


def test_always_ask_wins_over_protected_path():
    """When cost is fine, ALWAYS_ASK kind triggers before path check."""
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.DELETE,
            target_path="core/orchestrator.py",
            estimated_cost_usd=0.0,
        )
    )
    assert d.require_confirmation is True
    assert "удаление" in d.reason  # delete reason, not protected reason


# ---------------------------------------------------------------------------
# evaluate() — input validation
# ---------------------------------------------------------------------------


def test_evaluate_rejects_non_action_input():
    gate = ConfirmationGate()
    with pytest.raises(ValueError, match="invalid_action_type"):
        gate.evaluate("not an action")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# evaluate_batch
# ---------------------------------------------------------------------------


def test_evaluate_batch_returns_per_action_decisions():
    gate = ConfirmationGate()
    actions = [
        ActionDescriptor(kind=ActionKind.MODIFY, target_path="docs/x.md"),
        ActionDescriptor(kind=ActionKind.DELETE, target_path="docs/y.md"),
    ]
    batch = gate.evaluate_batch(actions)
    assert isinstance(batch, BatchDecision)
    assert len(batch.decisions) == 2
    assert batch.decisions[0].require_confirmation is False
    assert batch.decisions[1].require_confirmation is True
    assert batch.require_any_confirmation is True


def test_evaluate_batch_all_safe_returns_no_ask():
    gate = ConfirmationGate()
    actions = [
        ActionDescriptor(kind=ActionKind.MODIFY, target_path="docs/x.md"),
        ActionDescriptor(kind=ActionKind.WRITE_NEW, target_path="docs/y.md"),
    ]
    batch = gate.evaluate_batch(actions)
    assert batch.require_any_confirmation is False


def test_evaluate_batch_empty_iterable():
    gate = ConfirmationGate()
    batch = gate.evaluate_batch([])
    assert batch.decisions == ()
    assert batch.require_any_confirmation is False


def test_batch_asks_subset():
    gate = ConfirmationGate()
    actions = [
        ActionDescriptor(kind=ActionKind.MODIFY, target_path="docs/x.md"),
        ActionDescriptor(kind=ActionKind.DELETE, target_path="docs/y.md"),
        ActionDescriptor(kind=ActionKind.RENAME, target_path="docs/z.md"),
    ]
    batch = gate.evaluate_batch(actions)
    asks = batch.asks()
    assert len(asks) == 2
    assert all(d.require_confirmation for d in asks)


def test_evaluate_batch_accepts_generator():
    gate = ConfirmationGate()

    def gen():
        yield ActionDescriptor(kind=ActionKind.MODIFY, target_path="docs/a.md")
        yield ActionDescriptor(kind=ActionKind.DELETE, target_path="docs/b.md")

    batch = gate.evaluate_batch(gen())
    assert len(batch.decisions) == 2


# ---------------------------------------------------------------------------
# ConfirmationDecision / BatchDecision dataclass invariants
# ---------------------------------------------------------------------------


def test_confirmation_decision_is_frozen():
    a = ActionDescriptor(kind=ActionKind.MODIFY, target_path="x.py")
    d = ConfirmationDecision(require_confirmation=False, reason="r", action=a)
    with pytest.raises(Exception):
        d.require_confirmation = True  # type: ignore[misc]


def test_batch_decision_is_frozen():
    b = BatchDecision(decisions=(), require_any_confirmation=False)
    with pytest.raises(Exception):
        b.require_any_confirmation = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Reason text quality (Russian, informative)
# ---------------------------------------------------------------------------


def test_delete_reason_includes_path_and_recovery_warning():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(kind=ActionKind.DELETE, target_path="critical.yaml")
    )
    assert "critical.yaml" in d.reason
    assert "восстановление" in d.reason


def test_push_to_main_reason_explains_bypass():
    gate = ConfirmationGate()
    d = gate.evaluate(ActionDescriptor(kind=ActionKind.PUSH_TO_MAIN))
    assert "feature-branch" in d.reason
    assert "review" in d.reason


def test_protected_path_reason_includes_path_and_protected_zone():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="core/orchestrator.py",
            detail="adding state",
        )
    )
    assert "core/orchestrator.py" in d.reason
    assert "adding state" in d.reason


def test_high_cost_reason_includes_amount_and_threshold():
    gate = ConfirmationGate(cost_threshold_usd=2.5)
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.MODIFY,
            target_path="x.py",
            estimated_cost_usd=10.50,
        )
    )
    assert "$10.50" in d.reason
    assert "$2.50" in d.reason


def test_add_dependency_uses_default_target_when_not_specified():
    gate = ConfirmationGate()
    d = gate.evaluate(
        ActionDescriptor(
            kind=ActionKind.ADD_DEPENDENCY,
            detail="numpy",
        )
    )
    assert "requirements.txt" in d.reason
