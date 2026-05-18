from __future__ import annotations

from pathlib import Path


def test_local_pilot_task_execution_doc_exists() -> None:
    assert Path("docs/LOCAL_PILOT_TASK_EXECUTION.md").is_file()


def test_local_pilot_task_execution_doc_truthful_scope() -> None:
    text = Path("docs/LOCAL_PILOT_TASK_EXECUTION.md").read_text(
        encoding="utf-8"
    )

    assert "Outcome: `pilot task executed locally`" in text
    assert "pilot task execution partially blocked" not in text

    assert "/private/tmp/ai-dev-team-local-pilot-l04/" in text
    assert "sandbox_repo" in text
    assert "pilot_sandbox" in text
    assert "task-1779090504-4cd765" in text
    assert "feature/task-1779090504-4cd765" in text
    assert "be223a03f65b7153833d2dde2295f9a75d3f40e3" in text

    assert "square(x: int) -> int" in text
    assert "tests/test_example.py" in text
    assert "README.md" in text
    assert "Expected result before run" in text
    assert "Actual result" in text

    assert "/projects/pilot_sandbox" in text
    assert "/projects/pilot_sandbox/history" in text
    assert "/projects/pilot_sandbox/team" in text
    assert "/healthz" in text
    assert "/readyz" in text

    assert "live Docker-mounted main project" in text
    assert "assist-mode" in text
    assert "VPS/server deployment" in text
    assert "production serving" in text


def test_roadmap_links_local_pilot_execution_doc() -> None:
    roadmap = Path("docs/ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_PILOT_TASK_EXECUTION.md" in roadmap
    assert "`L0.4` — local pilot task on a sandbox repo" in roadmap
