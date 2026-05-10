# 📦 Pinchana Core

**Pinchana Core** is the foundational shared library for the Pinchana scraping ecosystem. it provides shared models, storage management, VPN control, and Docker orchestration logic used by both the gateway and the individual scraper modules.

---

## ✨ Key Components

### 1. Models (`pinchana_core.models`)
Defines the standard Pydantic v2 schemas for requests and responses, ensuring consistent data structures across all modules.
- `ScrapeRequest`: Standardizes input URL and options.
- `ScrapeResponse`: Uniform output format for media metadata.
- `MediaItem`: Represents an individual image or video in a gallery.

### 2. Storage (`pinchana_core.storage`)
An intelligent file-based storage system with a **Global Media Cache**.
- **LRU Eviction:** Automatically deletes the oldest media files when the cache exceeds a configurable size (e.g., 10GB).
- **Download Helpers:** Concurrent downloading of media with retry logic.
- **Path Resolution:** Standardized directory structure for cached media.

### 3. VPN Controller (`pinchana_core.vpn`)
Provides an interface to interact with [Gluetun](https://github.com/qdm12/gluetun).
- **Signal Rotation:** Triggers an immediate IP change via the Gluetun API.
- **Health Checks:** Monitors the status of the VPN tunnel.

### 4. Docker Manager (`pinchana_core.docker_manager`)
Handles the discovery and lifecycle of scraper modules running in Docker containers.
- **Dynamic Discovery:** Reads `modules.yaml` to identify available scrapers and their route patterns.
- **Lifecycle Control:** Can programmatically start, stop, or rebuild scraper containers.

### 5. Plugin Registry (`pinchana_core.plugins`)
A lightweight registry for "in-process" plugins, allowing modules to be imported directly into the server for easier development or single-binary deployments.

---

## 🛠 Usage

This package is intended to be used as a dependency in other Pinchana components. It is managed by `uv`.

### Installation (as a submodule)
```bash
uv add ../pinchana-core
```

---

## 📜 License

MIT
