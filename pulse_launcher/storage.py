from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_RUN: dict[str, Any] = {
    "base_url": "http://localhost:3001",
    "ws_url": "ws://localhost:3001",
    "symbol": "BTCUSDT",
    "stream_mode": "kline",
    "interval": "1m",
    "api_key": "replace-api-key",
    "api_secret": "replace-api-secret",
    "recv_window_ms": 5000,
    "ws_timeout_seconds": 35.0,
    "ws_reconnect_max_seconds": 30.0,
    "health_host": "127.0.0.1",
    "health_port": 9109,
    "queue_maxsize": 4096,
    "queue_offer_timeout_seconds": 0.25,
    "pending_poll_seconds": 1.0,
}

DEFAULT_BROKER: dict[str, Any] = {
    "adapter": "chronos_simulator",
}

DEFAULT_CHRONOS: dict[str, Any] = {
    "enabled": False,
    "ingest_path": "/api/v1/live/telemetry",
    "publish_interval_seconds": 2.0,
    "timeout_seconds": 8.0,
}

DEFAULT_CREDENTIALS: dict[str, Any] = {}


@dataclass(frozen=True)
class StrategyManifest:
    id: str
    display_name: str
    description: str
    entrypoint: str
    source_dir: Path
    manifest_path: Path
    default_strategy: dict[str, Any]
    default_run: dict[str, Any]
    default_broker: dict[str, Any]
    default_chronos: dict[str, Any]
    default_credentials: dict[str, Any]
    strategy_paths: list[str]


@dataclass(frozen=True)
class StrategyPreset:
    id: str
    display_name: str
    strategy_name: str | None
    strategy: dict[str, Any]
    run: dict[str, Any]
    broker: dict[str, Any]
    chronos: dict[str, Any]
    credentials: dict[str, Any]
    strategy_paths: list[str]


@dataclass(frozen=True)
class BrokerDescriptor:
    adapter_id: str
    display_name: str
    supported_stream_modes: tuple[str, ...]
    supports_chronos_sink: bool


BROKER_DESCRIPTORS: dict[str, BrokerDescriptor] = {
    "chronos_simulator": BrokerDescriptor(
        adapter_id="chronos_simulator",
        display_name="Chronos Simulator",
        supported_stream_modes=("kline", "aggTrade"),
        supports_chronos_sink=True,
    ),
}


class LauncherStorageError(RuntimeError):
    pass


def ensure_workspace(root: Path) -> None:
    (root / ".launcher").mkdir(parents=True, exist_ok=True)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError as exc:
        raise LauncherStorageError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LauncherStorageError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise LauncherStorageError(f"JSON root must be object: {path}")
    return payload


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in items:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _resolve_path_list(raw_values: list[Any], base_dir: Path, *, field_name: str) -> list[str]:
    resolved: list[str] = []
    for idx, raw in enumerate(raw_values):
        if not isinstance(raw, str) or not raw.strip():
            raise LauncherStorageError(
                f"{field_name}[{idx}] in {base_dir} must be a non-empty string"
            )
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        path = path.resolve()
        if not path.exists():
            raise LauncherStorageError(
                f"{field_name}[{idx}] does not exist: {path} (manifest base {base_dir})"
            )
        if not path.is_dir():
            raise LauncherStorageError(f"{field_name}[{idx}] is not a directory: {path}")
        resolved.append(str(path))
    return _dedupe_keep_order(resolved)


def _iter_strategy_dirs(catalog_dir: Path) -> list[Path]:
    direct_manifest = catalog_dir / "manifest.json"
    if direct_manifest.exists():
        return [catalog_dir]

    out: list[Path] = []
    for child in sorted(catalog_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if (child / "manifest.json").exists():
            out.append(child)
    return out


def _parse_manifest(manifest_path: Path) -> StrategyManifest:
    strategy_dir = manifest_path.parent
    payload = _read_json_file(manifest_path)

    strategy_id_raw = payload.get("id", strategy_dir.name)
    if not isinstance(strategy_id_raw, str) or not strategy_id_raw.strip():
        raise LauncherStorageError(f"manifest id must be a non-empty string: {manifest_path}")
    strategy_id = strategy_id_raw.strip()

    entrypoint_raw = payload.get("entrypoint", payload.get("strategy_name"))
    if not isinstance(entrypoint_raw, str) or not entrypoint_raw.strip():
        raise LauncherStorageError(
            f"manifest must define 'entrypoint' (or legacy 'strategy_name'): {manifest_path}"
        )
    entrypoint = entrypoint_raw.strip()

    default_strategy = dict(payload.get("default_strategy") or {})
    default_run = dict(payload.get("default_run") or {})
    default_broker = dict(payload.get("default_broker") or {})
    default_chronos = dict(payload.get("default_chronos") or payload.get("default_platform") or {})
    default_credentials = dict(payload.get("default_credentials") or {})

    include_source_dir = bool(payload.get("include_source_dir", True))
    raw_strategy_paths = payload.get("strategy_paths", [])
    if not isinstance(raw_strategy_paths, list):
        raise LauncherStorageError(
            f"'strategy_paths' must be an array in {manifest_path}"
        )

    strategy_paths = _resolve_path_list(
        list(raw_strategy_paths),
        strategy_dir,
        field_name="strategy_paths",
    )
    source_dir_resolved = strategy_dir.resolve()
    if include_source_dir:
        strategy_paths = _dedupe_keep_order([str(source_dir_resolved)] + strategy_paths)

    return StrategyManifest(
        id=strategy_id,
        display_name=str(payload.get("display_name") or strategy_id),
        description=str(payload.get("description") or ""),
        entrypoint=entrypoint,
        source_dir=source_dir_resolved,
        manifest_path=manifest_path.resolve(),
        default_strategy=default_strategy,
        default_run=default_run,
        default_broker=default_broker,
        default_chronos=default_chronos,
        default_credentials=default_credentials,
        strategy_paths=strategy_paths,
    )


def load_strategy_manifests(catalog_dirs: list[Path]) -> list[StrategyManifest]:
    manifests: list[StrategyManifest] = []
    by_id: dict[str, Path] = {}

    for raw_dir in catalog_dirs:
        catalog_dir = raw_dir.expanduser().resolve()
        if not catalog_dir.exists():
            raise LauncherStorageError(f"catalog dir does not exist: {catalog_dir}")
        if not catalog_dir.is_dir():
            raise LauncherStorageError(f"catalog path is not a directory: {catalog_dir}")

        for strategy_dir in _iter_strategy_dirs(catalog_dir):
            manifest_path = strategy_dir / "manifest.json"
            manifest = _parse_manifest(manifest_path)

            previous = by_id.get(manifest.id)
            if previous is not None:
                raise LauncherStorageError(
                    f"duplicated strategy id '{manifest.id}' in {manifest.manifest_path} and {previous}"
                )
            by_id[manifest.id] = manifest.manifest_path
            manifests.append(manifest)

    manifests.sort(key=lambda item: (item.display_name.lower(), item.id.lower()))
    return manifests


def load_presets_for_manifest(manifest: StrategyManifest) -> list[StrategyPreset]:
    presets_dir = manifest.source_dir / "presets"
    if not presets_dir.exists():
        return []

    presets: list[StrategyPreset] = []
    for path in sorted(presets_dir.glob("*.json"), key=lambda item: item.name.lower()):
        payload = _read_json_file(path)

        raw_paths = payload.get("strategy_paths", [])
        if not isinstance(raw_paths, list):
            raise LauncherStorageError(f"'strategy_paths' must be an array in {path}")
        strategy_paths = _resolve_path_list(
            list(raw_paths),
            manifest.source_dir,
            field_name="strategy_paths",
        )

        presets.append(
            StrategyPreset(
                id=path.stem,
                display_name=str(payload.get("display_name") or path.stem),
                strategy_name=(
                    str(payload.get("strategy_name"))
                    if payload.get("strategy_name") is not None
                    else None
                ),
                strategy=dict(payload.get("strategy") or {}),
                run=dict(payload.get("run") or {}),
                broker=dict(payload.get("broker") or {}),
                chronos=dict(payload.get("chronos") or payload.get("platform") or {}),
                credentials=dict(payload.get("credentials") or {}),
                strategy_paths=strategy_paths,
            )
        )
    return presets


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _sanitize_run_payload(run_payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(run_payload)
    for deprecated_key in (
        "session_id",
        "bot_id_hint",
        "auto_start_session",
        "stop_on_session_end",
        "session_poll_seconds",
    ):
        sanitized.pop(deprecated_key, None)
    return sanitized


def build_base_config(manifest: StrategyManifest) -> dict[str, Any]:
    run_payload = deep_merge(DEFAULT_RUN, manifest.default_run)
    run_payload = _sanitize_run_payload(run_payload)
    return {
        "strategy_name": manifest.entrypoint,
        "strategy_paths": list(manifest.strategy_paths),
        "run": run_payload,
        "broker": deep_merge(DEFAULT_BROKER, manifest.default_broker),
        "chronos": deep_merge(DEFAULT_CHRONOS, manifest.default_chronos),
        "credentials": deep_merge(DEFAULT_CREDENTIALS, manifest.default_credentials),
        "strategy": dict(manifest.default_strategy),
    }


def apply_preset(config: dict[str, Any], preset: StrategyPreset) -> dict[str, Any]:
    updated = dict(config)
    if preset.strategy_name:
        updated["strategy_name"] = preset.strategy_name

    updated["run"] = _sanitize_run_payload(
        deep_merge(dict(updated.get("run") or {}), preset.run)
    )
    updated["broker"] = deep_merge(dict(updated.get("broker") or {}), preset.broker)
    updated["chronos"] = deep_merge(dict(updated.get("chronos") or {}), preset.chronos)
    updated["credentials"] = deep_merge(
        dict(updated.get("credentials") or {}),
        preset.credentials,
    )
    updated["strategy"] = deep_merge(dict(updated.get("strategy") or {}), preset.strategy)

    merged_paths = list(updated.get("strategy_paths") or [])
    merged_paths.extend(preset.strategy_paths)
    updated["strategy_paths"] = _dedupe_keep_order(
        [str(item) for item in merged_paths if str(item).strip()]
    )
    return updated


def list_broker_descriptors() -> list[BrokerDescriptor]:
    return sorted(BROKER_DESCRIPTORS.values(), key=lambda item: item.display_name.lower())


def validate_effective_config(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    run_payload = payload.get("run")
    if not isinstance(run_payload, dict):
        return ["runtime config requires a 'run' object"]
    broker_payload = payload.get("broker")
    if not isinstance(broker_payload, dict):
        errors.append("runtime config requires a 'broker' object")
        return errors

    adapter = str(broker_payload.get("adapter") or "").strip().lower()
    if not adapter:
        errors.append("broker.adapter is required")
        return errors

    descriptor = BROKER_DESCRIPTORS.get(adapter)
    if descriptor is None:
        available = ", ".join(sorted(BROKER_DESCRIPTORS.keys()))
        errors.append(
            f"unsupported broker.adapter '{adapter}' (available: {available})"
        )
        return errors

    raw_stream_mode = str(run_payload.get("stream_mode") or "kline").strip().lower()
    if raw_stream_mode == "kline":
        stream_mode = "kline"
    elif raw_stream_mode in {"aggtrade", "agg_trade"}:
        stream_mode = "aggTrade"
    else:
        errors.append(f"unsupported run.stream_mode '{raw_stream_mode}'")
        return errors

    if stream_mode not in descriptor.supported_stream_modes:
        supported = ", ".join(descriptor.supported_stream_modes)
        errors.append(
            f"broker '{adapter}' does not support stream_mode '{stream_mode}' "
            f"(supported: {supported})"
        )

    chronos_payload = payload.get("chronos")
    if chronos_payload is None:
        chronos_payload = payload.get("platform")
    if chronos_payload is not None and not isinstance(chronos_payload, dict):
        errors.append("chronos/platform config must be an object when provided")
        return errors

    chronos_enabled = bool((chronos_payload or {}).get("enabled", False))
    if chronos_enabled and not descriptor.supports_chronos_sink:
        errors.append(
            f"broker '{adapter}' cannot publish Chronos telemetry sink"
        )

    return errors
