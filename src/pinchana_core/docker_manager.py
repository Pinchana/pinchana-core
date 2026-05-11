"""Docker container orchestration for pinchana scraper modules."""

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import yaml

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?}")


def _expand_env_string(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} placeholders in a string."""

    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        return os.getenv(name, default if default is not None else "")

    return _ENV_VAR_RE.sub(repl, value)


def _expand_env_tree(value):
    """Recursively expand env placeholders in nested YAML data structures."""
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, list):
        return [_expand_env_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env_tree(v) for k, v in value.items()}
    return value


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ContainerModule:
    """Configuration for a module that runs inside a Docker container."""

    name: str
    enabled: bool
    source_type: str          # 'git' or 'local'
    source_url: str           # git URL or local path
    route_patterns: list[str] = field(default_factory=list)
    endpoint: str = ""        # HTTP endpoint e.g. http://localhost:8081
    dockerfile: str = "Dockerfile"
    port: int = 8080
    image_tag: Optional[str] = None
    container_name: Optional[str] = None
    network: str = "container:gluetun"
    cache_volume: str = "scraper-cache"
    env: dict | None = None


class ContainerRegistry:
    """Read-only registry of pre-started container modules.

    Unlike ModuleContainerManager, this does NOT build or start containers.
    It reads the YAML config and assumes modules are already running
    (e.g. managed by docker-compose or Kubernetes).
    """

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.getenv(
            "MODULES_CONFIG", "/app/config/modules.yaml"
        )
        self.modules: dict[str, ContainerModule] = {}
        self._load_config()

    def _load_config(self) -> None:
        if not Path(self.config_path).exists():
            logger.warning("Module config not found: %s", self.config_path)
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = _expand_env_tree(yaml.safe_load(f) or {})

        for name, cfg in data.get("modules", {}).items():
            if not cfg.get("enabled", False):
                continue
            source = cfg.get("source", {})
            container = cfg.get("container", {})
            port = _to_int(container.get("port", 8080), 8080)
            endpoint = container.get("endpoint", f"http://localhost:{port}")
            self.modules[name] = ContainerModule(
                name=name,
                enabled=True,
                source_type=source.get("type", "local"),
                source_url=source.get("url") or source.get("path", ""),
                route_patterns=cfg.get("route_patterns", []),
                endpoint=endpoint,
                dockerfile=container.get("dockerfile", "Dockerfile"),
                port=port,
                image_tag=container.get("image_tag", f"pinchana-module-{name}"),
                container_name=container.get("container_name") or container.get("name") or f"pinchana-{name}",
                network=container.get("network", "container:gluetun"),
                cache_volume=container.get("cache_volume", "scraper-cache"),
                env=container.get("env", {}),
            )
        logger.info("Loaded %d container modules from config", len(self.modules))

    async def health(self, name: str) -> dict:
        """HTTP health check against the module's /health endpoint."""
        module = self.modules.get(name)
        if not module:
            return {"status": "not_configured"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{module.endpoint}/health")
                resp.raise_for_status()
                return {"status": "healthy", "detail": resp.json()}
        except Exception as e:
            return {"status": "unhealthy", "detail": str(e)}

    def list_modules(self) -> dict[str, dict]:
        """Return a snapshot of all configured modules."""
        return {
            name: {
                "endpoint": m.endpoint,
                "patterns": m.route_patterns,
            }
            for name, m in self.modules.items()
        }


class ModuleContainerManager:
    """Manages the lifecycle of scraper module Docker containers.

    Reads a YAML config, clones/pulls module sources, builds images,
    and starts/stops containers on demand.
    """

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.getenv(
            "MODULES_CONFIG", "/app/config/modules.yaml"
        )
        self.modules: dict[str, ContainerModule] = {}
        self.running: dict[str, dict] = {}
        self._load_config()

    def _load_config(self) -> None:
        if not Path(self.config_path).exists():
            logger.warning("Module config not found: %s", self.config_path)
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = _expand_env_tree(yaml.safe_load(f) or {})

        for name, cfg in data.get("modules", {}).items():
            if not cfg.get("enabled", False):
                continue
            source = cfg.get("source", {})
            container = cfg.get("container", {})
            port = _to_int(container.get("port", 8080), 8080)
            endpoint = container.get("endpoint", f"http://localhost:{port}")
            self.modules[name] = ContainerModule(
                name=name,
                enabled=True,
                source_type=source.get("type", "local"),
                source_url=source.get("url") or source.get("path", ""),
                route_patterns=cfg.get("route_patterns", []),
                endpoint=endpoint,
                dockerfile=container.get("dockerfile", "Dockerfile"),
                port=port,
                image_tag=container.get("image_tag", f"pinchana-module-{name}"),
                container_name=container.get("container_name") or container.get("name") or f"pinchana-{name}",
                network=container.get("network", "container:gluetun"),
                cache_volume=container.get("cache_volume", "scraper-cache"),
                env=container.get("env", {}),
            )
        logger.info("Loaded %d container modules from config", len(self.modules))

    def _prepare_source(self, module: ContainerModule) -> str:
        """Ensure the module source is available locally and return its path."""
        if module.source_type == "git":
            dest = f"/tmp/pinchana-modules/{module.name}"
            if Path(dest).exists():
                logger.info("Pulling latest for %s in %s", module.name, dest)
                subprocess.run(
                    ["git", "-C", dest, "pull"],
                    check=False, capture_output=True,
                )
            else:
                logger.info("Cloning %s into %s", module.source_url, dest)
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "clone", "--depth", "1", module.source_url, dest],
                    check=True,
                )
            return dest

        # local
        if not Path(module.source_url).exists():
            raise FileNotFoundError(
                f"Module source not found: {module.source_url}"
            )
        return module.source_url

    def build(self, name: str) -> str:
        """Build the Docker image for a module."""
        module = self.modules[name]
        build_path = self._prepare_source(module)
        dockerfile_path = str(Path(build_path) / module.dockerfile)

        logger.info(
            "Building image %s for module %s from %s",
            module.image_tag, name, build_path,
        )
        subprocess.run(
            ["docker", "build", "-t", module.image_tag, "-f", dockerfile_path, build_path],
            check=True,
        )
        return module.image_tag

    def start(self, name: str) -> str:
        """Start a module container and return its HTTP endpoint."""
        if name in self.running:
            logger.info("Module %s is already running", name)
            return self.running[name]["endpoint"]

        module = self.modules[name]

        # Build image if it doesn't exist yet
        try:
            subprocess.run(
                ["docker", "image", "inspect", module.image_tag],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            self.build(name)

        # Remove any stale container with the same name
        subprocess.run(
            ["docker", "rm", "-f", module.container_name],
            capture_output=True,
        )

        logger.info(
            "Starting container %s for module %s",
            module.container_name, name,
        )

        cmd = [
            "docker", "run", "-d",
            "--name", module.container_name,
            "--network", module.network,
            "-v", f"{module.cache_volume}:/app/cache",
            "-e", "CACHE_PATH=/app/cache",
            "-e", f"CACHE_MAX_SIZE_GB={os.getenv('CACHE_MAX_SIZE_GB', '10.0')}",
        ]
        # Add custom env vars
        for key, value in (module.env or {}).items():
            cmd.extend(["-e", f"{key}={value}"])
        cmd.append(module.image_tag)

        subprocess.run(cmd, check=True)

        # All module containers share the same network namespace (gluetun)
        # so the server (also on that namespace) can reach them via localhost.
        endpoint = module.endpoint or f"http://localhost:{module.port}"
        
        self.running[name] = {
            "container": module.container_name,
            "endpoint": endpoint,
            "module": module,
        }
        return endpoint

    def stop(self, name: str) -> None:
        """Stop and remove a module container."""
        if name not in self.running:
            return
        module = self.running[name]["module"]
        logger.info("Stopping container %s for module %s", module.container_name, name)
        subprocess.run(
            ["docker", "rm", "-f", module.container_name],
            capture_output=True,
        )
        del self.running[name]

    def stop_all(self) -> None:
        """Stop every running module container."""
        for name in list(self.running.keys()):
            self.stop(name)

    def health(self, name: str) -> dict:
        """Check Docker-level health of a module container."""
        if name not in self.running:
            return {"status": "not_running"}

        module = self.running[name]["module"]
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format={{.State.Status}}", module.container_name],
                capture_output=True, text=True, check=True,
            )
            return {
                "status": result.stdout.strip(),
                "endpoint": self.running[name]["endpoint"],
            }
        except subprocess.CalledProcessError:
            return {"status": "error"}

    def list_running(self) -> dict[str, dict]:
        """Return a snapshot of all running modules."""
        return {
            name: {
                "endpoint": info["endpoint"],
                "health": self.health(name),
            }
            for name, info in self.running.items()
        }
