# Pinchana Core

Pinchana Core is the shared Python library used by the Pinchana gateway and platform modules. It provides common request and legacy response models, cache storage, music extraction helpers, VPN control, module lifecycle support, and the in-process plugin registry.

## Main packages

- `pinchana_core.models` defines the shared Pydantic request and module response types.
- `pinchana_core.storage` downloads media, resolves cache paths, and evicts the oldest post directories when the configured size limit is exceeded.
- `pinchana_core.music` contains common behavior for the music platform modules.
- `pinchana_core.vpn` checks the tunnel, Gluetun DNS process, and a real DNS
  lookup before reporting the VPN ready, and safely reconnects the tunnel.
- `pinchana_core.docker_manager` reads module configuration and performs optional container lifecycle operations.
- `pinchana_core.plugins` registers modules that run inside the gateway process during development.

The gateway, rather than this package, converts module responses to the public API v1 `{data, meta}` contract.

## Development

Run commands from this directory:

```sh
uv sync --frozen
uv run python -c "import pinchana_core; print('pinchana_core imported')"
```

Other repository modules consume this package through a local uv path dependency. Docker builds must use the parent `pinchana-api` directory as their build context because module Dockerfiles copy both this package and the selected service.

The DNS readiness probe resolves `cloudflare.com` with a five-second timeout.
Direct deployments may override these defaults with
`VPN_DNS_PROBE_HOSTNAME` and `VPN_DNS_PROBE_TIMEOUT_SECONDS`.

## Security notes

Treat cached media as private operational data. Do not expose arbitrary cache paths, Gluetun control port 8000, or Docker lifecycle operations to the public Internet. `CONTAINER_MODE=false` disables lifecycle routes but does not remove an existing Docker socket mount.

## License

MIT. See `LICENSE` in the repository root.
