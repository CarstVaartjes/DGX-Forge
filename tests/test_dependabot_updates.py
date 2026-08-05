from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github/dependabot.yml"


def test_dependabot_covers_every_container_and_action_location_weekly() -> None:
    text = CONFIG.read_text()
    assert text.startswith("version: 2\n")
    assert 'package-ecosystem: "docker"' in text
    assert 'package-ecosystem: "docker-compose"' in text
    assert 'package-ecosystem: "github-actions"' in text
    for directory in (
        '      - "/control"',
        '      - "/deploy/compose/hermes-agent"',
        '      - "/deploy/compose"',
        '      - "/deploy/compose/tailscale"',
    ):
        assert directory in text
    assert text.count('interval: "weekly"') == 3
    assert "automerge" not in text.lower()
