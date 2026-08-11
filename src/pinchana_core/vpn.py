import httpx
import logging
import asyncio
import os
import time
from typing import Optional

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

    def __init__(
        self,
        control_url: Optional[str] = None,
        api_key: Optional[str] = None,
        rotation_cooldown: Optional[float] = None,
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

    async def get_vpn_status(self) -> dict:
        """Fetch current VPN status from Gluetun."""
        if not self.enabled:
            return {"status": "disabled", "enabled": False}
        client = self._client()
        try:
            resp = await client.get("/v1/vpn/status")
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise VpnRotationError(
                    f"Gluetun /v1/vpn/status returned non-dict JSON ({type(data).__name__}): {data!r}"
                )
            return data
        finally:
            await client.aclose()

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
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = await self.get_vpn_status()
                vpn_status = status.get("status", "").lower()

                if vpn_status == "running":
                    # Verification: ensure we can actually get a public IP.
                    # Gluetun reports 'running' as soon as the process starts,
                    # but the tunnel might still be performing MTU discovery or IP assignment.
                    ip = await self.get_public_ip()
                    if ip:
                        logger.info(f"VPN connected. Public IP: {ip}")
                        return status

                logger.debug(f"VPN not ready yet: status={vpn_status}")
            except httpx.HTTPStatusError as e:
                logger.debug(f"HTTP error polling VPN status: {e.response.status_code}")
            except httpx.ConnectError:
                logger.debug("Gluetun control server not reachable yet.")
            except httpx.HTTPError as e:
                logger.debug(f"Error polling VPN status: {e}")
            except VpnRotationError as e:
                logger.debug(f"VPN rotation polling issue: {e}")
            await asyncio.sleep(interval)
        raise VpnRotationError(
            f"VPN failed to connect within {timeout}s after rotation — possible transient AUTH_FAILED"
        )

    async def rotate_ip(self, *, wait_for_cooldown: bool = False) -> Optional[str]:
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
            logger.info("Rate limit hit. Reconnecting VPN tunnel...")
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
                raise
            except httpx.ConnectError:
                logger.error("Cannot connect to Gluetun control server. Is Gluetun running?")
                raise
            except httpx.HTTPError as e:
                logger.error(f"Failed to communicate with Gluetun control server: {e}")
                raise
            finally:
                await client.aclose()
