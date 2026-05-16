# Pulse Launcher

Pulse Launcher is a Textual terminal UI for preparing and running Pulse runtime
configs from strategy manifests.

It discovers strategy catalogs, merges presets into an effective Pulse config,
resolves credentials from an encrypted local keyring, previews the command that
will run, and starts/stops Pulse as a subprocess.

## Install

```bash
cd /path/to/pulse-launcher
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python -m pulse_launcher \
  --workspace /path/to/pulse-launcher \
  --catalog-dir /path/to/pulse-launcher/examples/strategy-catalog
```

For config-file setup, manifest schema, keyring behavior, and supported
environment variables, see [docs/quickstart.md](docs/quickstart.md).
