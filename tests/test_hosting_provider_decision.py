from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
DEPLOY_DOC_PATH = REPO_ROOT / "docs" / "DEPLOY_NEW_ARCHITECTURE.md"
HOSTING_DOC_PATH = REPO_ROOT / "docs" / "HOSTING_PROVIDER_DECISION.md"
ROADMAP_PATH = REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md"


def test_hosting_decision_doc_exists_and_navigation_points_to_it():
    assert HOSTING_DOC_PATH.exists() is True
    assert README_PATH.exists() is True
    assert DEPLOY_DOC_PATH.exists() is True
    assert ROADMAP_PATH.exists() is True

    readme = README_PATH.read_text(encoding="utf-8")

    assert "docs/HOSTING_PROVIDER_DECISION.md" in readme


def test_hosting_decision_doc_states_provider_plan_region_and_purchase_truth():
    text = HOSTING_DOC_PATH.read_text(encoding="utf-8")

    required_markers = (
        "Hetzner Cloud",
        "CPX22",
        "hel1",
        "not purchased yet",
        "purchase blocked externally",
        "Ubuntu 24.04 LTS",
        "C1.2",
    )

    for marker in required_markers:
        assert marker in text


def test_hosting_decision_doc_keeps_exact_chosen_provider_pricing_truth():
    text = HOSTING_DOC_PATH.read_text(encoding="utf-8")

    required_pricing_markers = (
        "EUR 7.99/mo",
        "EUR 0.50/mo",
        "EUR 8.49/mo",
    )

    for marker in required_pricing_markers:
        assert marker in text

    forbidden_pricing_markers = (
        "EUR 0.60/mo",
        "EUR 8.59/mo",
    )

    for marker in forbidden_pricing_markers:
        assert marker not in text


def test_hosting_decision_doc_stays_within_c11_scope():
    text = HOSTING_DOC_PATH.read_text(encoding="utf-8")

    implemented_limits = (
        "did **not**",
        "bootstrap Ubuntu",
        "configure `nginx`",
        "configure `systemd`",
        "deploy the Telegram runtime",
        "deploy the Web Office runtime",
        "configure HTTPS",
    )

    for marker in implemented_limits:
        assert marker in text

    phantom_done_markers = (
        "Ubuntu already configured",
        "nginx already configured",
        "systemd already configured",
        "bot already deployed",
        "web already deployed",
        "HTTPS already connected",
        "server purchased and provisioned",
    )

    for marker in phantom_done_markers:
        assert marker not in text


def test_hosting_decision_doc_references_real_files_and_commands():
    text = HOSTING_DOC_PATH.read_text(encoding="utf-8")

    referenced_paths = (
        "docs/DEPLOY_NEW_ARCHITECTURE.md",
        "docs/ROADMAP_TO_PRODUCTION.md",
        "scripts/run_telegram_bot.py",
        "scripts/backup_state_db.py",
        "web.main:app",
    )

    for relative_path in referenced_paths:
        assert relative_path in text
        if ":" not in relative_path:
            assert (REPO_ROOT / relative_path).exists() is True


def test_active_roadmap_does_not_conflict_with_canonical_hosting_decision():
    roadmap = ROADMAP_PATH.read_text(encoding="utf-8")

    required_markers = (
        "docs/HOSTING_PROVIDER_DECISION.md",
        "CPX22",
        "hel1",
        "€8.49/mo",
    )

    for marker in required_markers:
        assert marker in roadmap

    forbidden_markers = (
        "Hetzner CX22",
        "Создать сервер CX22",
        "€5.83/mo",
    )

    for marker in forbidden_markers:
        assert marker not in roadmap
