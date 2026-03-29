from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pulse-launcher" / "config.json"


class LauncherSettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class LauncherSettings:
    workspace: Path
    catalog_dirs: list[Path]
    pulse_cmd: str
    pulse_cwd: Path
    config_path: Path | None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LauncherSettingsError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise LauncherSettingsError(f"cannot read config file {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LauncherSettingsError(f"invalid JSON in config file {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise LauncherSettingsError(f"config root must be an object: {path}")
    return payload


def _resolve_path(raw: str, *, base_dir: Path | None = None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def _split_env_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(os.pathsep) if item.strip()]


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def resolve_launcher_settings(args: Any) -> LauncherSettings:
    env = os.environ

    config_path: Path | None = None
    if getattr(args, "config", None):
        config_path = _resolve_path(str(args.config))
    elif env.get("PULSE_LAUNCHER_CONFIG"):
        config_path = _resolve_path(env["PULSE_LAUNCHER_CONFIG"])
    elif DEFAULT_CONFIG_PATH.exists():
        config_path = DEFAULT_CONFIG_PATH

    config_payload: dict[str, Any] = {}
    config_base: Path | None = None
    if config_path is not None:
        config_payload = _read_json_object(config_path)
        config_base = config_path.parent

    workspace_raw = (
        getattr(args, "workspace", None)
        or env.get("PULSE_LAUNCHER_WORKSPACE")
        or config_payload.get("workspace")
        or "."
    )
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise LauncherSettingsError("workspace must be a non-empty string")
    workspace = _resolve_path(workspace_raw.strip(), base_dir=config_base)

    cli_catalogs: list[str] = list(getattr(args, "catalog_dir", []) or [])
    env_catalogs: list[str] = _split_env_paths(env.get("PULSE_LAUNCHER_CATALOG_DIRS"))
    config_catalogs_raw: list[str] = []
    if "catalog_dirs" in config_payload:
        raw_value = config_payload["catalog_dirs"]
        if not isinstance(raw_value, list):
            raise LauncherSettingsError("config field 'catalog_dirs' must be an array of strings")
        for idx, item in enumerate(raw_value):
            if not isinstance(item, str) or not item.strip():
                raise LauncherSettingsError(f"catalog_dirs[{idx}] must be a non-empty string")
            config_catalogs_raw.append(item.strip())
    elif "catalog_dir" in config_payload:
        raw_value = config_payload["catalog_dir"]
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise LauncherSettingsError("config field 'catalog_dir' must be a non-empty string")
        config_catalogs_raw = [raw_value.strip()]

    selected_catalogs = cli_catalogs or env_catalogs or config_catalogs_raw
    catalog_dirs = _dedupe_paths(
        [_resolve_path(item, base_dir=config_base) for item in selected_catalogs]
    )

    pulse_cmd_raw = (
        getattr(args, "pulse_cmd", None)
        or env.get("PULSE_LAUNCHER_PULSE_CMD")
        or config_payload.get("pulse_cmd")
        or "python -m pulse"
    )
    if not isinstance(pulse_cmd_raw, str) or not pulse_cmd_raw.strip():
        raise LauncherSettingsError("pulse_cmd must be a non-empty string")
    pulse_cmd = pulse_cmd_raw.strip()

    pulse_cwd_raw = (
        getattr(args, "pulse_cwd", None)
        or env.get("PULSE_LAUNCHER_PULSE_CWD")
        or config_payload.get("pulse_cwd")
        or str((workspace.parent / "pulse").resolve())
    )
    if not isinstance(pulse_cwd_raw, str) or not pulse_cwd_raw.strip():
        raise LauncherSettingsError("pulse_cwd must be a non-empty string")
    pulse_cwd = _resolve_path(pulse_cwd_raw.strip(), base_dir=config_base)

    return LauncherSettings(
        workspace=workspace,
        catalog_dirs=catalog_dirs,
        pulse_cmd=pulse_cmd,
        pulse_cwd=pulse_cwd,
        config_path=config_path,
    )
