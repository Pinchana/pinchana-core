"""Docker container orchestration for pinchana scraper modules."""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ContainerModule:
    """Configuration for a module that runs inside a Docker container."""

    name: str
    enabled: bool
    source_type: str          # 'git' or 'local'
    source_url: str           # git URL or local path
    dockerfile: str = "Dockerfile"
    port: int = 8080
    image_tag: Optional[str] = None
    container_name: Optional[str] = None
    network: str = "container:gluetun"
    cache_volume: str = "scraper-cache"
    env: dict | None = None


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
            data = yaml.safe_load(f) or {}

        for name, cfg in data.get("modules", {}).items():
            if not cfg.get("enabled", False):
                continue
            source = cfg.get("source", {})
            container = cfg.get("container", {})
            self.modules[name] = ContainerModule(
                name=name,
                enabled=True,
                source_type=source.get("type", "local"),
                source_url=source.get("url") or source.get("path", ""),
                dockerfile=container.get("dockerfile", "Dockerfile"),
                port=container.get("port", 8080),
                image_tag=container.get("image_tag", f"pinchana-module-{name}"),
                container_name=container.get("name", f"pinchana-{name}"),
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
        endpoint = f"http://localhost:{module.port}"
        
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
