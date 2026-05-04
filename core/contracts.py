from dataclasses import dataclass


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    violations: list[str]


FSM_REQUIRED_STATES = {
    "IDLE",
    "PLANNING",
    "PM",
    "ARCHITECT",
    "WRITER",
    "REVIEW",
    "TEST",
    "QA",
    "FIX",
    "SUCCESS",
    "FAIL",
    "BLOCKED",
}


def validate_fsm_states(states: list[str]) -> ContractResult:
    current = set(states)
    missing = sorted(FSM_REQUIRED_STATES - current)

    if missing:
        return ContractResult(
            ok=False,
            violations=[f"missing_state:{x}" for x in missing],
        )

    return ContractResult(ok=True, violations=[])


def validate_non_empty_text(value: str, field_name: str) -> ContractResult:
    if not value or not value.strip():
        return ContractResult(
            ok=False,
            violations=[f"empty_field:{field_name}"],
        )

    return ContractResult(ok=True, violations=[])


FORBIDDEN_PATH_PARTS = {
    ".venv",
    "__pycache__",
    ".git",
}

PROTECTED_FILES = {
    "core/agents.py",
    "core/fsm.py",
    "core/contracts.py",
}


def validate_file_change(path: str) -> ContractResult:
    for item in FORBIDDEN_PATH_PARTS:
        if item in path.split("/"):
            return ContractResult(
                ok=False,
                violations=[f"forbidden_path:{item}"],
            )

    if path in PROTECTED_FILES:
        return ContractResult(
            ok=False,
            violations=[f"protected_file:{path}"],
        )

    return ContractResult(ok=True, violations=[])


def validate_code_change(path: str, content: str) -> ContractResult:
    file_result = validate_file_change(path)
    if not file_result.ok:
        return file_result

    text_result = validate_non_empty_text(content, "content")
    if not text_result.ok:
        return text_result

    forbidden_tokens = [
        "TODO",
        "FIXME",
        "NotImplementedError",
        "placeholder",
    ]

    violations = [
        f"forbidden_token:{token}"
        for token in forbidden_tokens
        if token in content
    ]

    if violations:
        return ContractResult(ok=False, violations=violations)

    return ContractResult(ok=True, violations=[])


def enforce_code_change(path: str, content: str) -> None:
    result = validate_code_change(path, content)

    if not result.ok:
        raise ValueError(";".join(result.violations))
