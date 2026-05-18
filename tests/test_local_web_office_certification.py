from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_WEB_OFFICE_CERTIFICATION.md"


def test_local_web_office_certification_doc_exists():
    assert DOC_PATH.exists() is True


def test_local_web_office_certification_doc_covers_required_surfaces_truthfully():
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    required_markers = (
        "dashboard",
        "project view",
        "team view",
        "history view",
        "settings view",
        "/healthz",
        "/readyz",
        "127.0.0.1:8001",
        "127.0.0.1:8002",
        "no fake sample activity",
        "no fake live updates",
        "no backend api contracts were changed",
        "this certification does **not** claim any of the following:",
        "production hosting is already configured",
        "vps rollout is complete",
        "a live realtime operator console already exists",
    )

    for marker in required_markers:
        assert marker in text_lower

    outcomes = (
        "Web Office certified locally",
        "Web Office certification partially blocked",
    )
    assert sum(outcome in text for outcome in outcomes) == 1

    forbidden_markers = (
        "all pages are live-updating in realtime",
        "the app is already deployed to production",
        "the live Docker-mounted main project is already attached",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower
