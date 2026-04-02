from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Log, Select, Static, TextArea

from pulse_launcher.keyring import (
    KeyringError,
    build_strategy_fingerprint,
    find_by_selector,
    normalize_selector,
    read_encrypted_keyring,
    selector_missing_required,
    supported_provider,
    upsert_record,
    write_encrypted_keyring,
)
from pulse_launcher.settings import LauncherSettingsError, resolve_launcher_settings
from pulse_launcher.storage import (
    BrokerDescriptor,
    LauncherStorageError,
    StrategyManifest,
    StrategyPreset,
    apply_preset,
    build_base_config,
    ensure_workspace,
    list_broker_descriptors,
    load_presets_for_manifest,
    load_strategy_manifests,
    validate_effective_config,
)


class PulseLauncherApp(App[None]):
    CSS = """
    Screen {
      layout: vertical;
    }

    #toolbar {
      height: auto;
      padding: 0 1;
      border: round $surface;
    }

    #main {
      height: 1fr;
      padding: 0 1;
    }

    #left {
      width: 40;
      min-width: 36;
      height: 1fr;
      padding: 1;
      border: round $surface;
      overflow-y: auto;
    }

    #editor-pane {
      width: 1fr;
      height: 1fr;
      padding: 1;
      border: round $surface;
    }

    #right {
      width: 48;
      min-width: 44;
      height: 1fr;
      padding: 1;
      border: round $surface;
    }

    #strategy_select,
    #preset_select,
    #pulse_cmd,
    #pulse_cwd {
      margin-bottom: 1;
    }

    #catalog_dirs {
      min-height: 4;
      border: round $surface-darken-1;
      margin-bottom: 1;
      padding: 0 1;
    }

    #runtime_editor,
    #strategy_editor {
      height: 1fr;
      border: round $primary;
      margin-bottom: 1;
    }

    #command_preview {
      height: auto;
      min-height: 8;
      border: round $boost;
      padding: 0 1;
      margin-bottom: 1;
    }

    #process_log {
      height: 1fr;
      border: round $accent;
    }

    .section-label {
      margin-top: 1;
      margin-bottom: 0;
      color: $text-muted;
    }

    #status {
      margin-top: 1;
      min-height: 3;
      border: round $surface;
      padding: 0 1;
    }

    #actions,
    #catalog_actions {
      height: auto;
      margin-bottom: 1;
    }

    Button {
      margin-right: 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "safe_quit", "Quit"),
        ("q", "safe_quit", "Quit"),
    ]

    def __init__(
        self,
        workspace: Path,
        catalog_dirs: list[Path],
        *,
        default_pulse_cmd: str,
        default_pulse_cwd: Path,
        keyring_path: Path,
        keyring_passphrase_env: str,
        config_path: Path | None,
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.catalog_dirs = [Path(path).expanduser().resolve() for path in catalog_dirs]
        self.default_pulse_cmd = default_pulse_cmd
        self.default_pulse_cwd = default_pulse_cwd
        self.keyring_path = keyring_path.expanduser().resolve()
        self.keyring_passphrase_env = keyring_passphrase_env
        self.config_path = config_path
        self.manifests: dict[str, StrategyManifest] = {}
        self.presets_by_strategy: dict[str, list[StrategyPreset]] = {}
        self.broker_descriptors: list[BrokerDescriptor] = list_broker_descriptors()
        self._keyring_sync_enabled: bool = True
        self._process: asyncio.subprocess.Process | None = None
        self._stream_tasks: list[asyncio.Task[None]] = []
        self._watcher_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal(id="toolbar"):
            yield Input(value=self.default_pulse_cmd, placeholder="Pulse command", id="pulse_cmd")
            yield Input(
                value=str(self.default_pulse_cwd),
                placeholder="Pulse working directory",
                id="pulse_cwd",
            )
            yield Button("Refresh Catalog", id="refresh_btn")
            yield Button("Preview", id="preview_btn", variant="default")

        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label("Catalog Dirs", classes="section-label")
                yield Static("", id="catalog_dirs")

                yield Label("Strategy", classes="section-label")
                yield Select(
                    [("(no strategies found)", "__none__")],
                    allow_blank=False,
                    id="strategy_select",
                )
                yield Label("Preset", classes="section-label")
                yield Select(
                    [("(no preset)", "__none__")],
                    allow_blank=False,
                    id="preset_select",
                )
                yield Label("Broker Adapter", classes="section-label")
                broker_options = (
                    [(item.display_name, item.adapter_id) for item in self.broker_descriptors]
                    if self.broker_descriptors
                    else [("(no adapters)", "__none__")]
                )
                yield Select(
                    broker_options,
                    allow_blank=False,
                    id="broker_select",
                )
                yield Label("Chronos Telemetry", classes="section-label")
                yield Select(
                    [("Disabled", "disabled"), ("Enabled", "enabled")],
                    allow_blank=False,
                    id="chronos_select",
                )
                yield Label("Secret Manager Save", classes="section-label")
                yield Button(
                    "Save To Keyring: Enabled",
                    id="keyring_sync_btn",
                    variant="success",
                )

                with Horizontal(id="catalog_actions"):
                    yield Button("Load Base", id="load_base_btn")
                    yield Button("Apply Preset", id="apply_preset_btn")

                yield Static("Ready", id="status")

            with Vertical(id="editor-pane"):
                yield Label("Pulse Runtime Config (JSON)", classes="section-label")
                yield TextArea("{}", id="runtime_editor")
                yield Label("Strategy Params (JSON)", classes="section-label")
                yield TextArea("{}", id="strategy_editor")

            with Vertical(id="right"):
                yield Label("Command Preview", classes="section-label")
                yield Static("", id="command_preview")

                with Horizontal(id="actions"):
                    yield Button("Run", id="run_btn", variant="success")
                    yield Button("Stop", id="stop_btn", variant="error")

                yield Label("Process Log", classes="section-label")
                yield Log(id="process_log", highlight=True)

        yield Footer()

    def on_mount(self) -> None:
        try:
            ensure_workspace(self.workspace)
            self._render_catalog_dirs()
            self._reload_catalog()
        except LauncherStorageError as exc:
            self._set_status(str(exc), error=True)
        self._update_command_preview()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""

        if button_id == "refresh_btn":
            self._reload_catalog()
            return
        if button_id == "load_base_btn":
            self._load_base_from_selected()
            return
        if button_id == "apply_preset_btn":
            self._apply_selected_preset()
            return
        if button_id == "preview_btn":
            self._update_command_preview()
            return
        if button_id == "keyring_sync_btn":
            self._keyring_sync_enabled = not self._keyring_sync_enabled
            self._refresh_keyring_sync_button()
            self._apply_control_overrides()
            self._update_command_preview()
            return
        if button_id == "run_btn":
            await self._start_process()
            return
        if button_id == "stop_btn":
            await self._stop_process()
            return

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "strategy_select":
            self._refresh_preset_options()
            self._load_base_from_selected()
            return
        if event.select.id == "preset_select":
            value = event.value
            if value in (None, Select.BLANK, "__none__"):
                self._load_base_from_selected()
                return
            # Always apply presets from strategy base config so switching
            # presets yields deterministic effective configs.
            self._load_base_from_selected()
            self._apply_selected_preset()
            return
        if event.select.id in {"broker_select", "chronos_select"}:
            self._apply_control_overrides()
            self._update_command_preview()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id in {"runtime_editor", "strategy_editor"}:
            self._update_command_preview()

    async def action_safe_quit(self) -> None:
        await self._stop_process()
        self.exit()

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        if error:
            status.update(f"[b red]ERROR:[/b red] {message}")
        else:
            status.update(message)

    def _append_log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        log = self.query_one("#process_log", Log)
        log.write_line(f"[{ts}] {line}")

    def _render_catalog_dirs(self) -> None:
        panel = self.query_one("#catalog_dirs", Static)
        config_label = (
            f"config: {self.config_path}"
            if self.config_path is not None
            else "config: (none)"
        )
        keyring_label = (
            f"keyring: {self.keyring_path} (passphrase env: {self.keyring_passphrase_env})"
        )
        if not self.catalog_dirs:
            panel.update(
                f"{config_label}\n"
                f"{keyring_label}\n"
                "No catalog dirs configured.\n"
                "Use --catalog-dir, env, or config file."
            )
            return
        body = "\n".join(f"- {path}" for path in self.catalog_dirs)
        panel.update(f"{config_label}\n{keyring_label}\n{body}")

    def _reload_catalog(self) -> None:
        try:
            manifests = load_strategy_manifests(self.catalog_dirs)
        except LauncherStorageError as exc:
            self._set_status(str(exc), error=True)
            manifests = []

        self.manifests = {item.id: item for item in manifests}
        self.presets_by_strategy.clear()

        strategy_select = self.query_one("#strategy_select", Select)
        if not manifests:
            strategy_select.set_options([("(no strategies found)", "__none__")])
            strategy_select.value = "__none__"
            self._set_editor_config({})
            if self.catalog_dirs:
                self._set_status(
                    "No strategy manifests found in catalog dirs."
                )
            else:
                self._set_status(
                    "No catalog dirs. Start launcher with --catalog-dir."
                )
            return

        options = [(f"{manifest.display_name} ({manifest.id})", manifest.id) for manifest in manifests]
        strategy_select.set_options(options)
        strategy_select.value = manifests[0].id
        self._refresh_preset_options()
        self._load_base_from_selected()
        self._set_status(f"Loaded {len(manifests)} strategies")

    def _selected_strategy_id(self) -> str | None:
        strategy_select = self.query_one("#strategy_select", Select)
        value = strategy_select.value
        if value in (None, Select.BLANK, "__none__"):
            return None
        return str(value)

    def _selected_manifest(self) -> StrategyManifest | None:
        strategy_id = self._selected_strategy_id()
        if strategy_id is None:
            return None
        return self.manifests.get(strategy_id)

    def _selected_preset_id(self) -> str | None:
        preset_select = self.query_one("#preset_select", Select)
        value = preset_select.value
        if value in (None, Select.BLANK, "__none__"):
            return None
        return str(value)

    def _refresh_preset_options(self) -> None:
        preset_select = self.query_one("#preset_select", Select)
        manifest = self._selected_manifest()
        if manifest is None:
            preset_select.set_options([("(no preset)", "__none__")])
            preset_select.value = "__none__"
            return

        try:
            presets = load_presets_for_manifest(manifest)
        except LauncherStorageError as exc:
            self._set_status(str(exc), error=True)
            presets = []
        self.presets_by_strategy[manifest.id] = presets
        options: list[tuple[str, str]] = [("(no preset)", "__none__")]
        options.extend((preset.display_name, preset.id) for preset in presets)

        preset_select.set_options(options)
        preset_select.value = "__none__"

    def _set_editor_config(self, payload: dict[str, Any]) -> None:
        runtime_editor = self.query_one("#runtime_editor", TextArea)
        strategy_editor = self.query_one("#strategy_editor", TextArea)
        runtime_payload = {key: value for key, value in payload.items() if key != "strategy"}
        runtime_payload = self._sanitize_runtime_payload(runtime_payload)
        strategy_payload = payload.get("strategy")
        if not isinstance(strategy_payload, dict):
            strategy_payload = {}

        runtime_editor.load_text(json.dumps(runtime_payload, indent=2, ensure_ascii=True))
        strategy_editor.load_text(json.dumps(strategy_payload, indent=2, ensure_ascii=True))
        self._sync_control_selects(runtime_payload)

    def _sync_control_selects(self, runtime_payload: dict[str, Any]) -> None:
        broker_select = self.query_one("#broker_select", Select)
        chronos_select = self.query_one("#chronos_select", Select)

        broker_payload = runtime_payload.get("broker")
        if not isinstance(broker_payload, dict):
            broker_payload = {}
        adapter = str(broker_payload.get("adapter") or "chronos_simulator").strip().lower()
        known_adapter_ids = {item.adapter_id for item in self.broker_descriptors}
        if adapter in known_adapter_ids:
            broker_select.value = adapter

        chronos_payload = runtime_payload.get("chronos")
        if not isinstance(chronos_payload, dict):
            legacy_platform = runtime_payload.get("platform")
            chronos_payload = legacy_platform if isinstance(legacy_platform, dict) else {}
        chronos_enabled = bool(chronos_payload.get("enabled", False))
        chronos_select.value = "enabled" if chronos_enabled else "disabled"

        credentials_payload = runtime_payload.get("credentials")
        if not isinstance(credentials_payload, dict):
            credentials_payload = {}
        self._keyring_sync_enabled = bool(credentials_payload.get("save_to_keyring", True))
        self._refresh_keyring_sync_button()

    def _refresh_keyring_sync_button(self) -> None:
        button = self.query_one("#keyring_sync_btn", Button)
        if self._keyring_sync_enabled:
            button.label = "Save To Keyring: Enabled"
            button.variant = "success"
        else:
            button.label = "Save To Keyring: Disabled"
            button.variant = "warning"

    def _apply_control_overrides(self) -> None:
        runtime_editor = self.query_one("#runtime_editor", TextArea)
        try:
            runtime_payload = self._parse_editor_object(
                runtime_editor.text,
                label="Pulse runtime config",
            )
        except LauncherStorageError:
            return

        broker_select = self.query_one("#broker_select", Select)
        broker_value = broker_select.value
        if broker_value not in (None, Select.BLANK, "__none__"):
            broker_payload = runtime_payload.get("broker")
            if not isinstance(broker_payload, dict):
                broker_payload = {}
            broker_payload["adapter"] = str(broker_value)
            runtime_payload["broker"] = broker_payload

        chronos_select = self.query_one("#chronos_select", Select)
        chronos_payload = runtime_payload.get("chronos")
        if not isinstance(chronos_payload, dict):
            legacy_platform = runtime_payload.get("platform")
            chronos_payload = legacy_platform if isinstance(legacy_platform, dict) else {}
        chronos_payload["enabled"] = chronos_select.value == "enabled"
        runtime_payload["chronos"] = chronos_payload
        runtime_payload.pop("platform", None)

        credentials_payload = runtime_payload.get("credentials")
        if not isinstance(credentials_payload, dict):
            credentials_payload = {}
        credentials_payload["save_to_keyring"] = self._keyring_sync_enabled
        runtime_payload["credentials"] = credentials_payload

        runtime_editor.load_text(json.dumps(runtime_payload, indent=2, ensure_ascii=True))

    @staticmethod
    def _parse_editor_object(raw: str, *, label: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LauncherStorageError(f"invalid JSON in {label}: {exc}") from exc
        if not isinstance(payload, dict):
            raise LauncherStorageError(f"{label} root must be a JSON object")
        return payload

    @staticmethod
    def _sanitize_runtime_payload(runtime_payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(runtime_payload)
        run_payload = cleaned.get("run")
        if isinstance(run_payload, dict):
            run_clean = dict(run_payload)
            for deprecated_key in (
                "session_id",
                "bot_id_hint",
                "auto_start_session",
                "stop_on_session_end",
                "session_poll_seconds",
            ):
                run_clean.pop(deprecated_key, None)
            cleaned["run"] = run_clean
        return cleaned

    @staticmethod
    def _is_missing_secret(value: Any) -> bool:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return True
        placeholders = {
            "replace-api-key",
            "replace-api-secret",
            "<api-key>",
            "<api-secret>",
            "changeme",
        }
        return normalized in placeholders

    def _build_selector_payload(
        self,
        *,
        runtime_payload: dict[str, Any],
        credentials_payload: dict[str, Any],
    ) -> dict[str, str]:
        broker_payload = runtime_payload.get("broker")
        broker_adapter = ""
        if isinstance(broker_payload, dict):
            broker_adapter = str(broker_payload.get("adapter") or "").strip().lower()

        strategy_name = str(runtime_payload.get("strategy_name") or "").strip()
        strategy_payload = runtime_payload.get("strategy")
        if not isinstance(strategy_payload, dict):
            strategy_payload = {}

        try:
            strategy_fingerprint = build_strategy_fingerprint(
                strategy_name=strategy_name,
                strategy_payload=strategy_payload,
            )
        except KeyringError as exc:
            raise LauncherStorageError(str(exc)) from exc

        selector_payload = credentials_payload.get("selector")
        selector_raw: dict[str, Any] = {}
        if isinstance(selector_payload, dict):
            selector_raw = dict(selector_payload)
        elif selector_payload is not None:
            raise LauncherStorageError("credentials.selector must be an object when provided")

        venue = str(
            selector_raw.get("venue")
            or credentials_payload.get("venue")
            or broker_adapter
        ).strip().lower()

        deployment_id = str(
            selector_raw.get("deployment_id")
            or credentials_payload.get("deployment_id")
            or ""
        ).strip()

        strategy_fingerprint_value = str(
            selector_raw.get("strategy_fingerprint") or strategy_fingerprint
        ).strip().lower()

        return {
            "venue": venue,
            "strategy_fingerprint": strategy_fingerprint_value,
            "deployment_id": deployment_id,
        }

    @staticmethod
    def _save_to_keyring_enabled(credentials_payload: dict[str, Any]) -> bool:
        value = credentials_payload.get("save_to_keyring", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized not in {"0", "false", "no", "off"}
        return bool(value)

    def _resolve_credentials_for_runtime(
        self,
        payload: dict[str, Any],
        *,
        persist: bool,
    ) -> tuple[dict[str, Any], str]:
        runtime_payload = dict(payload)
        run_payload = dict(runtime_payload.get("run") or {})
        credentials_payload = runtime_payload.get("credentials")
        if not isinstance(credentials_payload, dict):
            runtime_payload["run"] = run_payload
            return runtime_payload, "manual (run.api_key/api_secret)"

        provider = str(credentials_payload.get("provider") or "local_encrypted_file").strip().lower()
        if not supported_provider(provider):
            raise LauncherStorageError(
                f"unsupported credentials.provider '{provider}'"
            )
        save_to_keyring_enabled = self._save_to_keyring_enabled(credentials_payload)

        try:
            selector = normalize_selector(
                self._build_selector_payload(
                    runtime_payload=runtime_payload,
                    credentials_payload=credentials_payload,
                )
            )
            missing_selector_fields = selector_missing_required(selector)
        except KeyringError as exc:
            raise LauncherStorageError(str(exc)) from exc

        if missing_selector_fields:
            fields = ", ".join(missing_selector_fields)
            raise LauncherStorageError(
                f"credentials.selector missing required fields: {fields}"
            )

        api_key = str(run_payload.get("api_key") or "").strip()
        api_secret = str(run_payload.get("api_secret") or "").strip()

        inline_api_key = str(credentials_payload.get("api_key") or "").strip()
        inline_api_secret = str(credentials_payload.get("api_secret") or "").strip()
        if (
            self._is_missing_secret(api_key)
            and self._is_missing_secret(api_secret)
            and inline_api_key
            and inline_api_secret
        ):
            api_key = inline_api_key
            api_secret = inline_api_secret

        has_manual_credentials = (
            not self._is_missing_secret(api_key)
            and not self._is_missing_secret(api_secret)
        )
        persist_effective = persist and save_to_keyring_enabled

        if has_manual_credentials and not persist_effective:
            run_payload["api_key"] = api_key
            run_payload["api_secret"] = api_secret
            runtime_payload["run"] = run_payload
            if save_to_keyring_enabled:
                return runtime_payload, "manual (run.api_key/api_secret)"
            return runtime_payload, "manual (keyring save disabled)"

        keyring_path_raw = str(credentials_payload.get("keyring_path") or "").strip()
        keyring_path = (
            Path(keyring_path_raw).expanduser().resolve()
            if keyring_path_raw
            else self.keyring_path
        )
        passphrase_env = str(
            credentials_payload.get("passphrase_env") or self.keyring_passphrase_env
        ).strip()
        if not passphrase_env:
            raise LauncherStorageError("credentials.passphrase_env must be a non-empty string")

        passphrase = os.environ.get(passphrase_env, "").strip()
        if not passphrase and not has_manual_credentials:
            raise LauncherStorageError(
                f"missing keyring passphrase env '{passphrase_env}'"
            )
        if not passphrase and has_manual_credentials:
            run_payload["api_key"] = api_key
            run_payload["api_secret"] = api_secret
            runtime_payload["run"] = run_payload
            return runtime_payload, "manual (keyring sync skipped: missing passphrase env)"

        try:
            records = read_encrypted_keyring(keyring_path, passphrase)
        except KeyringError as exc:
            raise LauncherStorageError(str(exc)) from exc

        if has_manual_credentials:
            run_payload["api_key"] = api_key
            run_payload["api_secret"] = api_secret
            runtime_payload["run"] = run_payload
            if persist_effective:
                label = str(credentials_payload.get("label") or "").strip()
                try:
                    next_records = upsert_record(
                        records,
                        selector=selector,
                        api_key=api_key,
                        api_secret=api_secret,
                        label=label,
                    )
                    write_encrypted_keyring(keyring_path, passphrase, next_records)
                except KeyringError as exc:
                    raise LauncherStorageError(str(exc)) from exc
            if persist_effective:
                return runtime_payload, f"manual + keyring ({keyring_path})"
            return runtime_payload, "manual (keyring save disabled)"

        try:
            resolved = find_by_selector(records, selector)
        except KeyringError as exc:
            raise LauncherStorageError(str(exc)) from exc
        if resolved is None:
            raise LauncherStorageError(
                "no credentials found in keyring for selector"
            )

        run_payload["api_key"] = resolved.api_key
        run_payload["api_secret"] = resolved.api_secret
        runtime_payload["run"] = run_payload
        return runtime_payload, f"keyring ({keyring_path})"

    def _read_editor_config(self) -> dict[str, Any]:
        runtime_editor = self.query_one("#runtime_editor", TextArea)
        strategy_editor = self.query_one("#strategy_editor", TextArea)

        runtime_payload = self._parse_editor_object(runtime_editor.text, label="Pulse runtime config")
        runtime_payload = self._sanitize_runtime_payload(runtime_payload)
        strategy_payload = self._parse_editor_object(strategy_editor.text, label="Strategy params")
        runtime_payload["strategy"] = strategy_payload
        return runtime_payload

    def _load_base_from_selected(self) -> None:
        manifest = self._selected_manifest()
        if manifest is None:
            return

        config = build_base_config(manifest)
        self._set_editor_config(config)
        self._set_status(f"Loaded base config for {manifest.display_name}")
        self._update_command_preview()

    def _apply_selected_preset(self) -> None:
        manifest = self._selected_manifest()
        if manifest is None:
            self._set_status("select a strategy first", error=True)
            return

        preset_id = self._selected_preset_id()
        if preset_id is None:
            self._set_status("no preset selected")
            return

        presets = self.presets_by_strategy.get(manifest.id, [])
        selected = next((preset for preset in presets if preset.id == preset_id), None)
        if selected is None:
            self._set_status(f"preset '{preset_id}' not found", error=True)
            return

        try:
            current = self._read_editor_config()
        except LauncherStorageError as exc:
            self._set_status(str(exc), error=True)
            return

        merged = apply_preset(current, selected)
        self._set_editor_config(merged)
        self._set_status(f"Applied preset '{selected.display_name}'")
        self._update_command_preview()

    def _effective_config_path(self) -> Path:
        return self.workspace / ".launcher" / "effective.json"

    def _build_command(self) -> tuple[list[str], Path]:
        pulse_cmd_raw = self.query_one("#pulse_cmd", Input).value.strip()
        if not pulse_cmd_raw:
            raise LauncherStorageError("Pulse command is empty")

        cwd_raw = self.query_one("#pulse_cwd", Input).value.strip()
        if not cwd_raw:
            raise LauncherStorageError("Pulse working directory is empty")

        cwd = Path(cwd_raw).expanduser()
        if not cwd.exists() or not cwd.is_dir():
            raise LauncherStorageError(f"Pulse working directory does not exist: {cwd}")

        try:
            prefix = shlex.split(pulse_cmd_raw)
        except ValueError as exc:
            raise LauncherStorageError(f"invalid Pulse command: {exc}") from exc
        if not prefix:
            raise LauncherStorageError("Pulse command produced empty argv")

        command = prefix + ["run", "--config", str(self._effective_config_path())]
        return command, cwd

    def _update_command_preview(self) -> None:
        preview = self.query_one("#command_preview", Static)
        try:
            config = self._read_editor_config()
            resolved_config, credentials_note = self._resolve_credentials_for_runtime(
                config,
                persist=False,
            )
            config_errors = validate_effective_config(resolved_config)
            if config_errors:
                raise LauncherStorageError("; ".join(config_errors))
            command, cwd = self._build_command()
            shell_line = " ".join(shlex.quote(arg) for arg in command)
            strategy_name = str(resolved_config.get("strategy_name") or "")
            strategy_paths = resolved_config.get("strategy_paths") or []
            paths_summary = ", ".join(str(item) for item in strategy_paths) or "(none)"
            broker_adapter = str((resolved_config.get("broker") or {}).get("adapter") or "(none)")
            chronos_enabled = bool((resolved_config.get("chronos") or {}).get("enabled", False))
            preview.update(
                f"$ {shell_line}\n"
                f"cwd: {cwd}\n"
                f"config: {self._effective_config_path()}\n"
                f"strategy_name: {strategy_name}\n"
                f"strategy_paths: {paths_summary}\n"
                f"broker: {broker_adapter}\n"
                f"chronos: {'enabled' if chronos_enabled else 'disabled'}\n"
                f"credentials: {credentials_note}"
            )
            self._set_status("Config valid")
        except LauncherStorageError as exc:
            preview.update(f"Cannot build command:\n{exc}")
            self._set_status(str(exc), error=True)

    async def _start_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._set_status("Pulse is already running")
            return

        try:
            config = self._read_editor_config()
            resolved_config, _ = self._resolve_credentials_for_runtime(
                config,
                persist=True,
            )
            config_errors = validate_effective_config(resolved_config)
            if config_errors:
                raise LauncherStorageError("; ".join(config_errors))
            command, cwd = self._build_command()
        except LauncherStorageError as exc:
            self._set_status(str(exc), error=True)
            return

        effective_path = self._effective_config_path()
        effective_path.parent.mkdir(parents=True, exist_ok=True)
        effective_path.write_text(
            json.dumps(resolved_config, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

        env = dict(os.environ)
        strategy_paths = [str(item).strip() for item in (resolved_config.get("strategy_paths") or []) if str(item).strip()]
        if strategy_paths:
            previous = env.get("PYTHONPATH", "")
            merged = strategy_paths + ([previous] if previous else [])
            env["PYTHONPATH"] = os.pathsep.join(merged)

        self._append_log("Launching Pulse runtime")
        self._append_log("$ " + " ".join(shlex.quote(item) for item in command))

        self._process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assert self._process.stdout is not None
        assert self._process.stderr is not None

        self._stream_tasks = [
            asyncio.create_task(self._pump_stream(self._process.stdout, "OUT")),
            asyncio.create_task(self._pump_stream(self._process.stderr, "ERR")),
        ]
        self._watcher_task = asyncio.create_task(self._watch_process())
        self._set_status("Pulse running")

    async def _pump_stream(self, stream: asyncio.StreamReader, channel: str) -> None:
        try:
            while True:
                chunk = await stream.readline()
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace").rstrip("\n")
                self._append_log(f"[{channel}] {text}")
        except asyncio.CancelledError:
            return

    async def _watch_process(self) -> None:
        if self._process is None:
            return
        rc = await self._process.wait()
        await asyncio.gather(*self._stream_tasks, return_exceptions=True)
        self._stream_tasks.clear()
        self._process = None
        self._watcher_task = None
        self._append_log(f"Pulse exited with code {rc}")
        if rc == 0:
            self._set_status("Pulse stopped")
        else:
            self._set_status(f"Pulse exited with code {rc}", error=True)

    async def _stop_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return

        self._append_log("Stopping Pulse process...")
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._append_log("Terminate timed out, killing process")
            process.kill()
            await process.wait()

        if self._watcher_task is not None:
            await self._watcher_task


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulse-launcher",
        description="Terminal launcher for Pulse runtime",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root (stores .launcher/effective.json and app state)",
    )
    parser.add_argument(
        "--catalog-dir",
        action="append",
        default=None,
        help=(
            "Strategy catalog directory. Can point to a directory containing either "
            "multiple strategy folders or a single strategy folder with manifest.json. "
            "Repeatable."
        ),
    )
    parser.add_argument(
        "--pulse-cmd",
        default=None,
        help="Default Pulse command shown in UI (override env/config).",
    )
    parser.add_argument(
        "--pulse-cwd",
        default=None,
        help="Default Pulse working directory shown in UI (override env/config).",
    )
    parser.add_argument(
        "--keyring-path",
        default=None,
        help="Encrypted keyring file path (override env/config).",
    )
    parser.add_argument(
        "--keyring-passphrase-env",
        default=None,
        help="Environment variable name that stores keyring passphrase.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Launcher config JSON path. If omitted, checks $PULSE_LAUNCHER_CONFIG "
            "then ~/.config/pulse-launcher/config.json"
        ),
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    try:
        settings = resolve_launcher_settings(args)
    except LauncherSettingsError as exc:
        print(f"[config-error] {exc}")
        return 2

    app = PulseLauncherApp(
        workspace=settings.workspace,
        catalog_dirs=settings.catalog_dirs,
        default_pulse_cmd=settings.pulse_cmd,
        default_pulse_cwd=settings.pulse_cwd,
        keyring_path=settings.keyring_path,
        keyring_passphrase_env=settings.keyring_passphrase_env,
        config_path=settings.config_path,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
