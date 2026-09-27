import json
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_UNIT = _ROOT / "deploy" / "cloudflared" / "workgate-cloudflared.service"


def test_cloudflared_service_keeps_workgate_lifecycle_separate() -> None:
    text = _UNIT.read_text(encoding="utf-8")

    assert "workgate control" not in text
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in text
    assert "User=cloudflared" in text
    assert "Group=cloudflared" in text
    assert "After=network.target" in text
    assert "network-online.target" not in text
    assert "Restart=on-failure" in text
    assert (
        "cloudflared tunnel --no-autoupdate run "
        "--token-file /etc/cloudflared/workgate.token"
    ) in text


def test_cloudflared_deploy_artifacts_are_in_source_distribution() -> None:
    config = tomllib.loads(
        (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    included = set(config["tool"]["uv"]["build-backend"]["source-include"])

    assert {
        "deploy/cloudflared/README.md",
        "deploy/cloudflared/workgate-cloudflared.service",
    } <= included


def test_legacy_cloudflare_surfaces_are_removed() -> None:
    assert not (_ROOT / "scripts" / "run-with-cloudflare-tunnel.sh").exists()

    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in env_example

    guide = (
        _ROOT / "docs" / "getting-started" / "cloudflare-tunnel.md"
    ).read_text(encoding="utf-8")
    assert "run-with-cloudflare-tunnel.sh" not in guide
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in guide
    assert "--token-file" in guide
    assert "config.yaml" in guide

    reference = json.loads(
        (
            _ROOT / "docs" / "reference" / "generated" / "configuration.json"
        ).read_text(encoding="utf-8")
    )
    assert "tunnel_helper_settings" not in reference
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in json.dumps(reference)
