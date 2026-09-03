import asyncio
import logging
import os
import socket
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class VpnRotationError(Exception):
    """Raised when the VPN tunnel fails to come up after rotation."""
    pass


class VpnRotationCooldownError(VpnRotationError):
    """Raised when callers request a reconnect before its cooldown expires."""

    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"VPN reconnect is cooling down; retry in {self.retry_after:.1f}s"
        )


class GluetunController:
    """Manage programmatic IP rotation via the Gluetun sidecar API."""

    ROTATION_COOLDOWN = 90  # seconds
    DNS_PROBE_HOSTNAME = "cloudflare.com"
    DNS_PROBE_TIMEOUT = 5.0

    def __init__(
        self,
        control_url: Optional[str] = None,
        api_key: Optional[str] = None,
        rotation_cooldown: Optional[float] = None,
        dns_probe_hostname: Optional[str] = None,
        dns_probe_timeout: Optional[float] = None,
    ):
        self.enabled = os.getenv("VPN_ENABLED", "true").lower() in ("1", "true", "yes")
        self.control_url = control_url or os.getenv("GLUETUN_CONTROL_URL", "http://localhost:8000")
        self.api_key = api_key or os.getenv("GLUETUN_API_KEY", "secret-key")
        configured_cooldown = (
            rotation_cooldown
            if rotation_cooldown is not None
            else float(
                os.getenv("VPN_ROTATION_COOLDOWN_SECONDS", self.ROTATION_COOLDOWN)
            )
        )
        self.rotation_cooldown = max(0.0, configured_cooldown)
        self.dns_probe_hostname = dns_probe_hostname or os.getenv(
            "VPN_DNS_PROBE_HOSTNAME", self.DNS_PROBE_HOSTNAME
        )
        configured_probe_timeout = (
            dns_probe_timeout
            if dns_probe_timeout is not None
            else float(os.getenv("VPN_DNS_PROBE_TIMEOUT_SECONDS", self.DNS_PROBE_TIMEOUT))
        )
        self.dns_probe_timeout = max(0.1, configured_probe_timeout)
        self._last_rotation = 0.0
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.control_url,
            headers={
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=15.0,
        )

    async def _control_status(self, path: str, label: str) -> dict:
        client = self._client()
        try:
            resp = await client.get(path)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise VpnRotationError(
                    f"Gluetun {label} endpoint returned non-dict JSON "
                    f"({type(data).__name__})"
                )
            return data
        except VpnRotationError:
            raise
        except httpx.HTTPError as exc:
            raise VpnRotationError(
                f"Failed to query Gluetun {label} status: {exc}"
            ) from exc
        finally:
            await client.aclose()

    async def _probe_dns(self) -> dict:
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.getaddrinfo(
                    self.dns_probe_hostname,
                    443,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                ),
                timeout=self.dns_probe_timeout,
            )
        except (TimeoutError, OSError) as exc:
            return {
                "status": "degraded",
                "hostname": self.dns_probe_hostname,
                "error": str(exc) or type(exc).__name__,
            }

        addresses = sorted({record[4][0] for record in records if record[4]})
        if not addresses:
            return {
                "status": "degraded",
                "hostname": self.dns_probe_hostname,
                "error": "resolver returned no IPv4 addresses",
            }
        return {
            "status": "running",
            "hostname": self.dns_probe_hostname,
            "addresses": addresses,
        }

    async def get_dns_status(self) -> dict:
        """Return Gluetun DNS state plus a real resolver probe."""
        if not self.enabled:
            return {"status": "disabled", "enabled": False}

        try:
            control = await self._control_status("/v1/dns/status", "DNS")
        except VpnRotationError as exc:
            return {"status": "degraded", "control": "unavailable", "error": str(exc)}

        control_status = str(control.get("status", "unknown")).lower()
        if control_status != "running":
            return {"status": "degraded", "control": control_status}

        probe = await self._probe_dns()
        return {
            "status": probe["status"],
            "control": control_status,
            "probe": probe,
        }

    async def get_vpn_status(self) -> dict:
        """Fetch VPN, Gluetun DNS, and resolver readiness.

        The top-level status remains ``running`` only when the tunnel and DNS
        are both ready. Existing service health checks therefore fail closed
        when Gluetun's DNS process is stopped or real resolution is broken.
        """
        if not self.enabled:
            return {"status": "disabled", "enabled": False}

        data = await self._control_status("/v1/vpn/status", "VPN")
        vpn_status = str(data.get("status", "unknown")).lower()
        result = dict(data)
        result["vpn_status"] = vpn_status
        if vpn_status != "running":
            return result

        dns = await self.get_dns_status()
        result["dns"] = dns
        if dns.get("status") != "running":
            result["status"] = "degraded"
        return result

    async def get_public_ip(self) -> Optional[str]:
        """Fetch current public IP from Gluetun."""
        if not self.enabled:
            return None
        client = self._client()
        try:
            resp = await client.get("/v1/publicip/ip")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data.get("public_ip")
            return None
        except Exception:
            return None
        finally:
            await client.aclose()

    async def wait_for_connection(self, timeout: float = 60.0, interval: float = 2.0) -> dict:
        """Poll Gluetun until the VPN is connected or timeout expires."""
        if not self.enabled:
            return {"status": "disabled", "enabled": False}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = await self.get_vpn_status()
                vpn_status = status.get("status", "").lower()

                if vpn_status == "running":
                    # get_vpn_status already verified the Gluetun DNS process
                    # and a real hostname lookup. Also require public egress.
                    ip = await self.get_public_ip()
                    if ip:
                        logger.info(f"VPN connected. Public IP: {ip}")
                        return status

                logger.debug(f"VPN not ready yet: status={vpn_status}")
            except VpnRotationError as e:
                logger.debug(f"VPN rotation polling issue: {e}")
            await asyncio.sleep(interval)
        raise VpnRotationError(
            f"VPN failed to connect within {timeout}s after rotation — possible transient AUTH_FAILED"
        )

    async def rotate_ip(
        self,
        *,
        wait_for_cooldown: bool = False,
        reason: str = "upstream block or connectivity failure",
    ) -> Optional[str]:
        """Reconnect the VPN tunnel and return its resulting public IP.

        Gluetun reconnects the configured endpoint; it does not guarantee that
        the provider assigns a different public IP. Callers can compare the
        returned value with their previously observed IP when that matters.
        """
        if not self.enabled:
            logger.info("VPN rotation skipped because VPN_ENABLED=false")
            return None

        requested_from_ip = await self.get_public_ip()
        async with self._lock:
            current_ip = await self.get_public_ip()
            if requested_from_ip and current_ip and current_ip != requested_from_ip:
                logger.info(
                    "VPN egress already changed while reconnect request was queued: %s -> %s",
                    requested_from_ip,
                    current_ip,
                )
                return current_ip

            now = time.monotonic()
            elapsed = now - self._last_rotation
            if elapsed < self.rotation_cooldown:
                retry_after = self.rotation_cooldown - elapsed
                if not wait_for_cooldown:
                    raise VpnRotationCooldownError(retry_after)
                logger.info(
                    "VPN reconnect on cooldown; waiting %.1fs before reconnecting",
                    retry_after,
                )
                await asyncio.sleep(retry_after)

                current_ip = await self.get_public_ip()
                if requested_from_ip and current_ip and current_ip != requested_from_ip:
                    logger.info(
                        "VPN egress changed during cooldown wait: %s -> %s",
                        requested_from_ip,
                        current_ip,
                    )
                    return current_ip

            previous_ip = current_ip or requested_from_ip
            logger.info("Reconnecting VPN tunnel: %s", reason)
            client = self._client()
            try:
                stop_resp = await client.put("/v1/vpn/status", json={"status": "stopped"})
                stop_resp.raise_for_status()

                # WireGuard is stateless; no need for a long disconnect delay.
                await asyncio.sleep(1.0)

                start_resp = await client.put("/v1/vpn/status", json={"status": "running"})
                start_resp.raise_for_status()

                # Wait until the tunnel is actually connected instead of blind sleep
                await self.wait_for_connection(timeout=60.0, interval=2.0)

                current_ip = await self.get_public_ip()
                self._last_rotation = time.monotonic()
                if previous_ip and current_ip == previous_ip:
                    logger.warning(
                        "VPN tunnel reconnected but public IP did not change: %s",
                        current_ip,
                    )
                else:
                    logger.info(
                        "VPN tunnel reconnected; public IP: %s -> %s",
                        previous_ip or "unknown",
                        current_ip or "unknown",
                    )
                return current_ip
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    logger.error(f"Gluetun auth failed ({e.response.status_code}). Check GLUETUN_API_KEY.")
                else:
                    logger.error(f"Gluetun control API error: {e.response.status_code}")
                raise VpnRotationError(
                    f"Gluetun control API returned HTTP {e.response.status_code}"
                ) from e
            except httpx.ConnectError as e:
                logger.error("Cannot connect to Gluetun control server. Is Gluetun running?")
                raise VpnRotationError("Cannot connect to Gluetun control server") from e
            except httpx.HTTPError as e:
                logger.error(f"Failed to communicate with Gluetun control server: {e}")
                raise VpnRotationError(
                    f"Failed to communicate with Gluetun control server: {e}"
                ) from e
            finally:
                await client.aclose()
