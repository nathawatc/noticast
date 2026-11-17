# Noticast

Share your currently playing Linux media (MPRIS2) to Instagram Music Notes
automatically or from the CLI.

> [!CAUTION]
> This tool is for educational and personal experimentation purposes only.
> Automated interactions with private APIs carry inherent risks, including
> rate limits, temporary restrictions, security checkpoints, or account
> suspension. Use responsibly at your own risk and respect the terms of
> service of the platforms you interact with.

## Requirements

- Linux with Python `3.14+`
- Active D-Bus session bus (MPRIS2-compatible media player)

## Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml` to customize your device/region profile, MPRIS filters
(`allowed_url_prefixes`, `max_length_seconds`), and anti-bot delays.

## Usage

Make sure your virtual environment is active (`source .venv/bin/activate`):

```bash
# 1. Authenticate once (supports 2FA) -> saves session.json
python main.py setup

# 2. Mode 1: Manual search & post
python main.py manual

# 3. Mode 2: MPRIS Daemon (use --dry-run first to test)
python main.py daemon --dry-run
python main.py daemon

# 4. Clear active Note or log out & remove session.json
python main.py clear
python main.py logout

# Optional: -v / --verbose for debug output
# (or install `noticast` globally inside your venv via `pip install -e .`)
python main.py -v daemon --dry-run
```
