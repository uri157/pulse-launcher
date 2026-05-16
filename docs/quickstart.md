# Pulse Launcher (Textual)

Terminal UI launcher for Pulse runtime.

## Install

```bash
cd /path/to/pulse-launcher
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (direct flags)

```bash
python -m pulse_launcher \
  --workspace /path/to/pulse-launcher \
  --catalog-dir /abs/path/to/your-strategies-catalog
```

## Run (config file, recommended)

Create config file once:

```bash
mkdir -p ~/.config/pulse-launcher
cp /path/to/pulse-launcher/launcher.config.example.json \
  ~/.config/pulse-launcher/config.json
```

Edit `workspace`, `catalog_dirs`, and `pulse_cwd` in that file for your local
checkout.

Then launch with no flags:

```bash
export PULSE_LAUNCHER_KEYRING_PASSPHRASE="replace-with-strong-passphrase"
python -m pulse_launcher
```

You can also point to another config file:

```bash
python -m pulse_launcher --config /abs/path/launcher.config.json
```

Priority order:

- CLI flags
- ENV vars
- config file
- internal defaults

## Workspace and catalog contract

Launcher writes local runtime artifacts under `--workspace`:

- `.launcher/effective.json` (generated at runtime)
- `.launcher/runs/` (subprocess logs and run state)
- `.launcher/copied_*.json` (fallback files when clipboard integration is unavailable)

Launcher discovers strategies from one or many `--catalog-dir` paths:

- `<catalog-dir>/<strategy-id>/manifest.json` + optional `presets/*.json`
- or `<catalog-dir>/manifest.json` + optional `presets/*.json` (single-strategy dir)

Manifests stay with strategy code and define `entrypoint` (or legacy
`strategy_name`) plus optional `strategy_paths`.

### Manifest minimum schema

```json
{
  "id": "ema_cross",
  "display_name": "EMA Cross",
  "entrypoint": "ema_cross",
  "default_strategy": { "fast_period": 40, "slow_period": 200, "quantity": 0.001 },
  "default_run": { "symbol": "BTCUSDT", "interval": "1m" },
  "default_broker": { "adapter": "chronos_simulator" },
  "default_chronos": { "enabled": true },
  "default_credentials": {
    "provider": "local_encrypted_file",
    "venue": "chronos_simulator",
    "save_to_keyring": true
  },
  "strategy_paths": [],
  "include_source_dir": true
}
```

Use an external entrypoint such as `my_strategy_package.builder:build_strategy`
with `strategy_paths` when the strategy is not built into Pulse.
When `include_source_dir` is true, the manifest directory is added to
`strategy_paths` automatically.

## What it does

- Discovers strategies from manifests.
- Loads presets and merges them over the current JSON config.
- Lets you choose broker adapter and Chronos telemetry mode from dedicated controls.
- Lets you edit the effective JSON config directly.
- Resolves credentials from encrypted local keyring using `venue` + auto-derived strategy fingerprint.
- Lets you enable/disable key persistence with `credentials.save_to_keyring` (and a UI control).
- Shows the final command used to run Pulse.
- Validates adapter capabilities before Preview/Run.
- Executes Pulse as subprocess and tails logs in UI.
- Stops process cleanly on `Stop` or `q` / `Ctrl+C`.

## Pulse command defaults

UI defaults:

- `Pulse command`: `python -m pulse`
- `Pulse working directory`: sibling `../pulse`

You can change both fields in the launcher before running.

## Local sample catalog (optional)

Repository ships a sample catalog only as reference:

- `/path/to/pulse-launcher/examples/strategy-catalog`

You can test it with:

```bash
python -m pulse_launcher \
  --workspace /path/to/pulse-launcher \
  --catalog-dir /path/to/pulse-launcher/examples/strategy-catalog
```

## Environment variables

Optional ENV configuration:

- `PULSE_LAUNCHER_CONFIG`
- `PULSE_LAUNCHER_WORKSPACE`
- `PULSE_LAUNCHER_CATALOG_DIRS` (multiple paths separated by `:` on Linux)
- `PULSE_LAUNCHER_PULSE_CMD`
- `PULSE_LAUNCHER_PULSE_CWD`
- `PULSE_LAUNCHER_KEYRING_PATH`
- `PULSE_LAUNCHER_KEYRING_PASSPHRASE_ENV`
- `PULSE_LAUNCHER_KEYRING_PASSPHRASE`
- `PULSE_LAUNCHER_CLIPBOARD_CMD`
- `PULSE_LAUNCHER_ENABLE_OSC52`
