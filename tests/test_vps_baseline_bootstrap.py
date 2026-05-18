from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_DOC_PATH = REPO_ROOT / "docs" / "VPS_BASELINE_BOOTSTRAP.md"
ROADMAP_PATH = REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md"


def test_vps_baseline_bootstrap_doc_exists_and_roadmap_points_to_it():
    assert BOOTSTRAP_DOC_PATH.exists() is True
    assert ROADMAP_PATH.exists() is True

    roadmap = ROADMAP_PATH.read_text(encoding="utf-8")
    assert "docs/VPS_BASELINE_BOOTSTRAP.md" in roadmap


def test_vps_baseline_bootstrap_doc_covers_c12_truth_surface():
    text = BOOTSTRAP_DOC_PATH.read_text(encoding="utf-8")

    required_markers = (
        "Hetzner Cloud",
        "CPX22",
        "hel1",
        "Ubuntu 24.04 LTS",
        "python3",
        "python3-venv",
        "nginx",
        "gh",
        "systemd",
        "1 x Cloud Primary IPv4",
        "not purchased yet",
        "bootstrap blocked externally",
        "server name",
        "server ID",
        "public IPv4",
        "SSH",
        "root",
        "sudo",
        "C1.3",
    )

    for marker in required_markers:
        assert marker in text

    outcomes = (
        "server baseline ready",
        "bootstrap blocked externally",
    )
    assert sum(outcome in text for outcome in outcomes) == 1


def test_vps_baseline_bootstrap_doc_truthfully_states_not_done_yet():
    text = BOOTSTRAP_DOC_PATH.read_text(encoding="utf-8")

    not_done_markers = (
        "bot backend deployed",
        "Web Office deployed",
        "app `.env` installed on the server",
        "app-specific systemd units created",
        "nginx reverse proxy configured for the app",
        "domain connected",
        "HTTPS enabled",
        "server-side backup automation enabled",
        "server-side healthchecks enabled",
    )

    for marker in not_done_markers:
        assert marker in text

    phantom_done_markers = (
        "Telegram runtime already deployed on the VPS",
        "Web Office already deployed on the VPS",
        "domain / HTTPS already connected",
        "app systemd units already running",
    )

    for marker in phantom_done_markers:
        assert marker not in text
