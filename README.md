# 🔋 Battery Monitor

Ultra-low-resource battery/system monitor for Termux on Android, aimed at Samsung Tab A devices.

## Features

- CPU usage from `/proc/stat` with no busy-wait and no `dumpsys` on normal runs
- RAM and swap usage
- Battery percentage, health, temperature, voltage and power
- Smoothed battery drain/charge rate
- Estimated time until empty / full
- ETA confidence indicator
- Storage usage
- Linux thermal-zone monitoring
- Network-state check
- Device/Android information
- High CPU/RAM/temperature alerts
- Discord battery and system webhooks
- Discord 429 handling and exponential backoff
- Monitor's own CPU/RAM usage
- Automatic slower polling under heavy load
- Webhook secrets stored locally in `.env`

## Installation

```bash
pkg update
pkg install python termux-api git
pip install -r requirements.txt
git clone https://github.com/therealmangoosey/Battery-monitor.git
cd Battery-monitor
cp .env.example .env
nano .env
python monitor.py
```

You need the **Termux:API** Android app as well as the `termux-api` package so `termux-battery-status` is available.

## Webhooks

Edit `.env`:

```env
BATTERY_WEBHOOK_URL=your-private-battery-webhook
SYSTEM_WEBHOOK_URL=your-private-system-webhook
```

Do **not** put real webhook URLs in `monitor.py`, `.env.example`, README files, issues, or commits.

### Why `.env` survives `git pull`

`.env` is deliberately **not tracked by Git**. It is listed in `.gitignore`, while only `.env.example` is committed.

That means this normal workflow is safe:

```bash
git pull --ff-only
```

Git updates tracked project files but leaves your local ignored `.env` alone. A future version of `monitor.py` can change without overwriting your webhook configuration.

**Important:** never commit `.env`. If an old clone already has `.env` tracked, remove it from Git tracking before using this setup.

## CPU sampling

The first CPU reading intentionally says `Sampling...`. CPU percentage requires two `/proc/stat` snapshots. The next loop calculates the percentage from the difference between them.

The monitor does not use a tight sampling loop. It sleeps between runs, and expensive device/storage/thermal information is refreshed only periodically.

## Battery ETA

ETA is based on recent battery percentage samples rather than a single reading. It needs enough elapsed time before it reports a meaningful result. This prevents wild estimates such as `20 hours remaining` after only a few seconds of data.

## Resource usage

The monitor is designed to stay idle most of the time. Normal operation uses:

- `/proc/stat` for CPU
- `/proc/meminfo` for RAM
- `termux-battery-status` only when a battery update is due
- periodic thermal/storage/device refreshes
- Discord requests only on their configured intervals

When CPU/RAM is high, system updates automatically slow down to 60 seconds; extreme load slows them to 120 seconds.

## Updating

```bash
git pull --ff-only
```

Your `.env` remains local and ignored.

## Security

If a Discord webhook was ever pasted into a chat, issue, screenshot, public repository, or log, treat it as compromised and rotate/delete that webhook in Discord. The repository intentionally contains no real webhook secrets.
