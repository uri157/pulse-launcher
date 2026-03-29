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

from pulse_launcher.settings import LauncherSettingsError, resolve_launcher_settings
from pulse_launcher.storage import (
    LauncherStorageError,
    StrategyManifest,
    StrategyPreset,
    apply_preset,
    build_base_config,
    ensure_workspace,
    load_presets_for_manifest,
    load_strategy_manifests,
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
      padding: 1;
      border: round $surface;
    }

    #editor-pane {
      width: 1fr;
      padding: 1;
      border: round $surface;
    }

    #right {
      width: 48;
      min-width: 44;
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

    #config_editor {
      height: 1fr;
      border: round $primary;
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
        config_path: Path | None,
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.catalog_dirs = [Path(path).expanduser().resolve() for path in catalog_dirs]
        self.default_pulse_cmd = default_pulse_cmd
        self.default_pulse_cwd = default_pulse_cwd
        self.config_path = config_path
        self.manifests: dict[str, StrategyManifest] = {}
        self.presets_by_strategy: dict[str, list[StrategyPreset]] = {}
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

                with Horizontal(id="catalog_actions"):
                    yield Button("Load Base", id="load_base_btn")
                    yield Button("Apply Preset", id="apply_preset_btn")

                yield Static("Ready", id="status")

            with Vertical(id="editor-pane"):
                yield Label("Effective Config (JSON)", classes="section-label")
                yield TextArea("{}", id="config_editor")

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

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "config_editor":
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
        if not self.catalog_dirs:
            panel.update(
                f"{config_label}\n"
                "No catalog dirs configured.\n"
                "Use --catalog-dir, env, or config file."
            )
            return
        body = "\n".join(f"- {path}" for path in self.catalog_dirs)
        panel.update(f"{config_label}\n{body}")

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
        editor = self.query_one("#config_editor", TextArea)
        editor.load_text(json.dumps(payload, indent=2, ensure_ascii=True))

    def _read_editor_config(self) -> dict[str, Any]:
        editor = self.query_one("#config_editor", TextArea)
        raw = editor.text
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LauncherStorageError(f"invalid JSON config: {exc}") from exc
        if not isinstance(payload, dict):
            raise LauncherStorageError("effective config root must be a JSON object")
        return payload

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
            command, cwd = self._build_command()
            shell_line = " ".join(shlex.quote(arg) for arg in command)
            strategy_name = str(config.get("strategy_name") or "")
            strategy_paths = config.get("strategy_paths") or []
            paths_summary = ", ".join(str(item) for item in strategy_paths) or "(none)"
            preview.update(
                f"$ {shell_line}\n"
                f"cwd: {cwd}\n"
                f"config: {self._effective_config_path()}\n"
                f"strategy_name: {strategy_name}\n"
                f"strategy_paths: {paths_summary}"
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
            command, cwd = self._build_command()
        except LauncherStorageError as exc:
            self._set_status(str(exc), error=True)
            return

        effective_path = self._effective_config_path()
        effective_path.parent.mkdir(parents=True, exist_ok=True)
        effective_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

        env = dict(os.environ)
        strategy_paths = [str(item).strip() for item in (config.get("strategy_paths") or []) if str(item).strip()]
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
        config_path=settings.config_path,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
