import time

import pytest

from pinchana_core.vpn import GluetunController, VpnRotationCooldownError


@pytest.mark.asyncio
async def test_vpn_status_is_running_when_tunnel_and_dns_are_ready(monkeypatch):
    monkeypatch.setenv("VPN_ENABLED", "true")
    controller = GluetunController()

    async def control_status(path: str, _label: str):
        assert path == "/v1/vpn/status"
        return {"status": "running"}

    async def dns_status():
        return {
            "status": "running",
            "control": "running",
            "probe": {"status": "running", "hostname": "cloudflare.com"},
        }

    monkeypatch.setattr(controller, "_control_status", control_status)
    monkeypatch.setattr(controller, "get_dns_status", dns_status)

    result = await controller.get_vpn_status()

    assert result["status"] == "running"
    assert result["vpn_status"] == "running"
    assert result["dns"]["status"] == "running"


@pytest.mark.asyncio
async def test_vpn_status_is_degraded_when_dns_probe_fails(monkeypatch):
    monkeypatch.setenv("VPN_ENABLED", "true")
    controller = GluetunController()

    async def control_status(path: str, _label: str):
        if path == "/v1/vpn/status":
            return {"status": "running"}
        if path == "/v1/dns/status":
            return {"status": "running"}
        raise AssertionError(path)

    async def failed_probe():
        return {
            "status": "degraded",
            "hostname": "cloudflare.com",
            "error": "resolver timed out",
        }

    monkeypatch.setattr(controller, "_control_status", control_status)
    monkeypatch.setattr(controller, "_probe_dns", failed_probe)

    result = await controller.get_vpn_status()

    assert result["status"] == "degraded"
    assert result["vpn_status"] == "running"
    assert result["dns"]["control"] == "running"
    assert result["dns"]["probe"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_disabled_vpn_does_not_probe_control_or_dns(monkeypatch):
    monkeypatch.setenv("VPN_ENABLED", "false")
    controller = GluetunController()

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled VPN must not perform health requests")

    monkeypatch.setattr(controller, "_control_status", unexpected)
    monkeypatch.setattr(controller, "_probe_dns", unexpected)

    assert await controller.get_vpn_status() == {
        "status": "disabled",
        "enabled": False,
    }


@pytest.mark.asyncio
async def test_reconnect_cooldown_is_explicit(monkeypatch):
    monkeypatch.setenv("VPN_ENABLED", "true")
    controller = GluetunController(rotation_cooldown=30)
    controller._last_rotation = time.monotonic()

    async def current_ip():
        return "203.0.113.10"

    monkeypatch.setattr(controller, "get_public_ip", current_ip)

    with pytest.raises(VpnRotationCooldownError) as exc_info:
        await controller.rotate_ip()

    assert 29 <= exc_info.value.retry_after <= 30
