import json
import os
import platform
import resource
import shutil
import subprocess
import time
from collections import deque

import requests

# ============================================================
# LOW-RESOURCE TERMUX / SAMSUNG BATTERY MONITOR
# Secrets are loaded from .env and are NEVER hard-coded here.
# ============================================================

SYSTEM_INTERVAL = 30
BATTERY_INTERVAL = 60
HIGH_LOAD_INTERVAL = 60
EXTREME_LOAD_INTERVAL = 120

HIGH_CPU = 50.0
EXTREME_CPU = 80.0
HIGH_RAM = 85.0
EXTREME_RAM = 95.0
LOW_BATTERY = 15
CRITICAL_BATTERY = 5
HIGH_TEMP = 42.0
EXTREME_TEMP = 48.0

TREND_WINDOW = 6 * 60 * 60
MAX_HISTORY = 16
ALERT_COOLDOWN = 15 * 60
DEVICE_REFRESH = 30 * 60
STORAGE_REFRESH = 5 * 60
THERMAL_REFRESH = 5 * 60


def load_env(path=".env"):
    """Tiny .env loader so python-dotenv is not required."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"'")
                if key:
                    values[key] = value
    except FileNotFoundError:
        pass

    for key, value in values.items():
        os.environ.setdefault(key, value)


load_env()

BATTERY_WEBHOOK_URL = os.getenv("BATTERY_WEBHOOK_URL", "").strip()
SYSTEM_WEBHOOK_URL = os.getenv("SYSTEM_WEBHOOK_URL", "").strip()

session = requests.Session()
start_time = time.monotonic()

cpu_previous = None
cpu_now = None
cpu_history = deque(maxlen=20)
cpu_average = None
cpu_peak = 0.0

ram_history = deque(maxlen=20)
ram_average = None
ram_peak = 0.0

battery_history = deque(maxlen=MAX_HISTORY)
charge_history = deque(maxlen=MAX_HISTORY)
battery_eta = "Collecting data..."
battery_rate = "Collecting data..."
battery_confidence = "Low"
charge_eta = "Collecting data..."
charge_rate = "Collecting data..."

last_battery = None
device_info = {}
storage_info = None
thermal_info = []
network_online = None

last_system_post = 0.0
last_battery_post = 0.0
last_device_refresh = 0.0
last_storage_refresh = 0.0
last_thermal_refresh = 0.0

alert_times = {}
webhook_backoff = {"system": 0.0, "battery": 0.0}
webhook_failures = {"system": 0, "battery": 0}

process_cpu_previous = None
process_wall_previous = None
monitor_cpu = 0.0
monitor_rss_mb = 0.0


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def command(args, timeout=2):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def eta(hours):
    if hours is None or hours < 0 or hours > 1000:
        return "Unknown"
    h = int(hours)
    m = int((hours - h) * 60)
    return f"~{h}h {m}m"


def get_cpu_usage():
    """Cheap CPU sampling using /proc/stat. No subprocess and no busy-wait."""
    global cpu_previous, cpu_now, cpu_average, cpu_peak
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
        if not parts or parts[0] != "cpu":
            return cpu_now
        values = [int(x) for x in parts[1:]]
        if len(values) < 4:
            return cpu_now
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        current = (total, idle)
        if cpu_previous is None:
            cpu_previous = current
            return None
        old_total, old_idle = cpu_previous
        cpu_previous = current
        total_delta = total - old_total
        idle_delta = idle - old_idle
        if total_delta <= 0:
            return cpu_now
        value = max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))
        cpu_now = round(value, 1)
        cpu_history.append(cpu_now)
        cpu_average = round(sum(cpu_history) / len(cpu_history), 1)
        cpu_peak = max(cpu_peak, cpu_now)
        return cpu_now
    except (OSError, ValueError, IndexError):
        return cpu_now


def get_ram():
    global ram_average, ram_peak
    try:
        total = available = swap_total = swap_free = 0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
                elif line.startswith("SwapTotal:"):
                    swap_total = int(line.split()[1])
                elif line.startswith("SwapFree:"):
                    swap_free = int(line.split()[1])
        if total <= 0:
            return None
        used = total - available
        percent = round(used * 100 / total, 1)
        ram_history.append(percent)
        ram_average = round(sum(ram_history) / len(ram_history), 1)
        ram_peak = max(ram_peak, percent)
        return {
            "percent": percent,
            "used_mb": round(used / 1024, 1),
            "total_mb": round(total / 1024, 1),
            "swap_used_mb": round((swap_total - swap_free) / 1024, 1),
            "swap_total_mb": round(swap_total / 1024, 1),
        }
    except Exception:
        return None


def get_battery():
    try:
        p = subprocess.run(["termux-battery-status"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=3)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return json.loads(p.stdout)
    except Exception:
        return None


def add_battery_sample(battery):
    if not battery:
        return
    pct = safe_float(battery.get("percentage"))
    if pct is None:
        return
    status = str(battery.get("status", "")).lower()
    now = time.monotonic()
    if status in ("charging", "charged", "full"):
        charge_history.append((now, pct))
        battery_history.clear()
    else:
        battery_history.append((now, pct))
        charge_history.clear()
    cutoff = now - TREND_WINDOW
    while battery_history and battery_history[0][0] < cutoff:
        battery_history.popleft()
    while charge_history and charge_history[0][0] < cutoff:
        charge_history.popleft()


def trend(history):
    if len(history) < 2:
        return None
    first = history[0][0]
    points = [(t - first, p) for t, p in history]
    if points[-1][0] < 120:
        return None
    xm = sum(x for x, _ in points) / len(points)
    ym = sum(y for _, y in points) / len(points)
    numerator = sum((x - xm) * (y - ym) for x, y in points)
    denominator = sum((x - xm) ** 2 for x, _ in points)
    if denominator <= 0:
        return None
    return numerator / denominator


def update_battery_estimate(battery):
    global battery_eta, battery_rate, battery_confidence, charge_eta, charge_rate
    if not battery:
        return
    pct = safe_float(battery.get("percentage"))
    if pct is None:
        return
    status = str(battery.get("status", "")).lower()
    if status in ("charging", "charged", "full"):
        battery_eta = "Not discharging"
        battery_rate = "Charging"
        battery_confidence = "N/A"
        slope = trend(charge_history)
        if slope is None or slope <= 0:
            charge_eta = "Collecting data..."
            charge_rate = "Collecting data..."
            return
        rate = slope * 3600
        charge_rate = f"+{rate:.2f}%/hour"
        charge_eta = eta((100 - pct) / rate)
        return
    slope = trend(battery_history)
    if slope is None or slope >= 0:
        battery_eta = "Collecting data..."
        battery_rate = "Collecting data..."
        battery_confidence = "Low"
        return
    rate = -slope * 3600
    if rate <= 0:
        return
    battery_eta = eta(pct / rate)
    battery_rate = f"{rate:.2f}%/hour"
    span = (battery_history[-1][0] - battery_history[0][0]) / 60
    if len(battery_history) >= 6 and span >= 15:
        battery_confidence = "High"
    elif len(battery_history) >= 3 and span >= 5:
        battery_confidence = "Medium"
    else:
        battery_confidence = "Low"


def get_storage():
    try:
        total, used, free = shutil.disk_usage("/")
        return {"total": round(total / 1024**3, 1), "used": round(used / 1024**3, 1),
                "free": round(free / 1024**3, 1), "percent": round(used * 100 / total, 1)}
    except Exception:
        return None


def get_thermal():
    zones = []
    try:
        base = "/sys/class/thermal"
        for name in os.listdir(base):
            if not name.startswith("thermal_zone"):
                continue
            try:
                with open(os.path.join(base, name, "temp")) as f:
                    temp = float(f.read().strip())
                if abs(temp) > 200:
                    temp /= 1000
                with open(os.path.join(base, name, "type")) as f:
                    zone_type = f.read().strip()
                if -50 <= temp <= 150:
                    zones.append({"type": zone_type, "temperature": round(temp, 1)})
            except Exception:
                pass
    except Exception:
        pass
    return zones


def max_thermal():
    return max(thermal_info, key=lambda x: x["temperature"]) if thermal_info else None


def refresh_device():
    info = {"machine": platform.machine(), "python": platform.python_version()}
    props = {"model": "ro.product.model", "manufacturer": "ro.product.manufacturer",
             "android": "ro.build.version.release", "sdk": "ro.build.version.sdk"}
    for key, prop in props.items():
        value = command(["getprop", prop])
        if value:
            info[key] = value
    return info


def get_network():
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "00000000":
                    return bool(int(parts[3], 16) & 1)
    except Exception:
        pass
    return None


def update_self_usage():
    global process_cpu_previous, process_wall_previous, monitor_cpu, monitor_rss_mb
    now = time.monotonic()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_time = usage.ru_utime + usage.ru_stime
    if process_cpu_previous is not None:
        wall = now - process_wall_previous
        if wall > 0:
            monitor_cpu = max(0.0, (cpu_time - process_cpu_previous) / wall * 100)
    process_cpu_previous = cpu_time
    process_wall_previous = now
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    monitor_rss_mb = int(line.split()[1]) / 1024
                    break
    except Exception:
        pass


def alert_once(name):
    now = time.monotonic()
    if now - alert_times.get(name, 0) < ALERT_COOLDOWN:
        return False
    alert_times[name] = now
    return True


def get_alerts(cpu, ram, battery):
    alerts = []
    if cpu is not None:
        if cpu >= EXTREME_CPU and alert_once("extreme_cpu"):
            alerts.append(f"🚨 CPU extremely high: {cpu:.1f}%")
        elif cpu >= HIGH_CPU and alert_once("high_cpu"):
            alerts.append(f"🔥 CPU high: {cpu:.1f}%")
    if ram:
        if ram["percent"] >= EXTREME_RAM and alert_once("extreme_ram"):
            alerts.append(f"🚨 RAM extremely high: {ram['percent']:.1f}%")
        elif ram["percent"] >= HIGH_RAM and alert_once("high_ram"):
            alerts.append(f"🧠 RAM high: {ram['percent']:.1f}%")
    if battery:
        pct = safe_float(battery.get("percentage"))
        temp = safe_float(battery.get("temperature"))
        if pct is not None:
            if pct <= CRITICAL_BATTERY and alert_once("critical_battery"):
                alerts.append(f"🚨 Battery critical: {pct:.0f}%")
            elif pct <= LOW_BATTERY and alert_once("low_battery"):
                alerts.append(f"🪫 Battery low: {pct:.0f}%")
        if temp is not None:
            if temp >= EXTREME_TEMP and alert_once("extreme_temp"):
                alerts.append(f"🚨 Battery temperature: {temp:.1f}°C")
            elif temp >= HIGH_TEMP and alert_once("high_temp"):
                alerts.append(f"🌡️ Battery temperature high: {temp:.1f}°C")
    zone = max_thermal()
    if zone and zone["temperature"] >= 55 and alert_once("thermal"):
        alerts.append(f"🔥 Thermal zone high: {zone['type']} {zone['temperature']:.1f}°C")
    return alerts


def send_webhook(kind, url, embed):
    if not url or time.monotonic() < webhook_backoff[kind]:
        return False
    try:
        r = session.post(url, json={"embeds": [embed]}, timeout=5)
        if r.status_code == 204:
            webhook_failures[kind] = 0
            webhook_backoff[kind] = 0
            return True
        if r.status_code == 429:
            try:
                retry = float(r.json().get("retry_after", 5))
            except Exception:
                retry = 5
            webhook_backoff[kind] = time.monotonic() + min(retry, 900)
            return False
        raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as exc:
        webhook_failures[kind] += 1
        webhook_backoff[kind] = time.monotonic() + min(2 ** min(webhook_failures[kind], 8), 900)
        print(f"[!] {kind} webhook: {exc}")
        return False


def system_embed(cpu, ram, alerts):
    zone = max_thermal()
    thermal = f"{zone['type']}: {zone['temperature']:.1f}°C" if zone else "Unavailable"
    storage = (f"{storage_info['used']} / {storage_info['total']} GB ({storage_info['percent']}%)"
               if storage_info else "Unavailable")
    model = device_info.get("model", "Android")
    android = device_info.get("android", "?")
    fields = [
        {"name": "CPU", "value": f"**{cpu:.1f}%**" if cpu is not None else "**Sampling...**", "inline": True},
        {"name": "CPU Average", "value": f"{cpu_average:.1f}%" if cpu_average is not None else "N/A", "inline": True},
        {"name": "CPU Peak", "value": f"{cpu_peak:.1f}%", "inline": True},
        {"name": "RAM", "value": f"**{ram['percent']:.1f}%** ({ram['used_mb']:.0f}/{ram['total_mb']:.0f} MB)" if ram else "Unavailable", "inline": True},
        {"name": "RAM Average", "value": f"{ram_average:.1f}%" if ram_average is not None else "N/A", "inline": True},
        {"name": "RAM Peak", "value": f"{ram_peak:.1f}%", "inline": True},
        {"name": "Swap", "value": f"{ram['swap_used_mb']:.0f}/{ram['swap_total_mb']:.0f} MB" if ram else "N/A", "inline": True},
        {"name": "Storage", "value": storage, "inline": True},
        {"name": "Thermal", "value": thermal, "inline": True},
        {"name": "Network", "value": "Online" if network_online is True else "Offline" if network_online is False else "Unknown", "inline": True},
        {"name": "Monitor CPU", "value": f"{monitor_cpu:.3f}%", "inline": True},
        {"name": "Monitor RAM", "value": f"{monitor_rss_mb:.2f} MB", "inline": True},
        {"name": "Device", "value": f"{model}\nAndroid {android}", "inline": True},
        {"name": "Uptime", "value": duration(time.monotonic() - start_time), "inline": True},
        {"name": "Updated", "value": f"<t:{int(time.time())}:R>", "inline": False},
    ]
    embed = {"title": "💻 System Monitor", "color": 15158332 if cpu is not None and cpu >= HIGH_CPU else 3447003,
             "fields": fields, "footer": {"text": "Termux Monitor • Ultra Low Resource Mode"}}
    if alerts:
        embed["description"] = "\n".join(alerts)
    return embed


def battery_embed(battery, alerts):
    voltage = safe_float(battery.get("voltage"))
    current = safe_float(battery.get("current"))
    power = f"{abs(voltage * current) / 1_000_000:.2f} W" if voltage is not None and current is not None else "Unavailable"
    pct = safe_float(battery.get("percentage"))
    color = 15158332 if pct is not None and pct <= CRITICAL_BATTERY else 16776960 if pct is not None and pct <= LOW_BATTERY else 3066993
    embed = {"title": "🔋 Battery Monitor", "color": color, "fields": [
        {"name": "Battery", "value": f"**{battery.get('percentage', '?')}%**", "inline": True},
        {"name": "Status", "value": f"**{battery.get('status', '?')}**", "inline": True},
        {"name": "Health", "value": str(battery.get("health", "Unknown")), "inline": True},
        {"name": "Temperature", "value": f"{battery.get('temperature', '?')} °C", "inline": True},
        {"name": "Voltage", "value": f"{battery.get('voltage', '?')} mV", "inline": True},
        {"name": "Power", "value": power, "inline": True},
        {"name": "Plugged", "value": str(battery.get("plugged", False)), "inline": True},
        {"name": "Battery ETA", "value": battery_eta, "inline": True},
        {"name": "ETA Confidence", "value": battery_confidence, "inline": True},
        {"name": "Drain Rate", "value": battery_rate, "inline": True},
        {"name": "Charge Rate", "value": charge_rate, "inline": True},
        {"name": "Full Charge ETA", "value": charge_eta, "inline": True},
        {"name": "Updated", "value": f"<t:{int(time.time())}:R>", "inline": False},
    ], "footer": {"text": "Termux Monitor • Smoothed Battery Analysis"}}
    if alerts:
        embed["description"] = "\n".join(alerts)
    return embed


def main():
    global last_system_post, last_battery_post
    global last_device_refresh, last_storage_refresh, last_thermal_refresh
    global device_info, storage_info, thermal_info, network_online, last_battery

    if not BATTERY_WEBHOOK_URL and not SYSTEM_WEBHOOK_URL:
        print("[!] No webhooks configured. Copy .env.example to .env and add them.")

    print("Starting Termux Monitor • Ultra Low Resource Mode")
    get_cpu_usage()  # prime CPU counter; first reading is intentionally Sampling...
    device_info = refresh_device()
    storage_info = get_storage()
    thermal_info = get_thermal()
    network_online = get_network()
    now = time.monotonic()
    last_device_refresh = last_storage_refresh = last_thermal_refresh = now

    while True:
        loop_start = time.monotonic()
        try:
            cpu = get_cpu_usage()
            ram = get_ram()
            now = time.monotonic()

            if now - last_device_refresh >= DEVICE_REFRESH:
                device_info = refresh_device()
                last_device_refresh = now
            if now - last_storage_refresh >= STORAGE_REFRESH:
                storage_info = get_storage()
                network_online = get_network()
                last_storage_refresh = now
            if now - last_thermal_refresh >= THERMAL_REFRESH:
                thermal_info = get_thermal()
                last_thermal_refresh = now

            if last_battery is None or now - last_battery_post >= BATTERY_INTERVAL:
                battery = get_battery()
                if battery:
                    last_battery = battery
                    add_battery_sample(battery)
                    update_battery_estimate(battery)

            battery = last_battery
            alerts = get_alerts(cpu, ram, battery)
            extreme = ((cpu is not None and cpu >= EXTREME_CPU) or
                        (ram is not None and ram["percent"] >= EXTREME_RAM))
            high = ((cpu is not None and cpu >= HIGH_CPU) or
                    (ram is not None and ram["percent"] >= HIGH_RAM) or
                    (battery is not None and (safe_float(battery.get("temperature")) or 0) >= HIGH_TEMP))
            interval = EXTREME_LOAD_INTERVAL if extreme else HIGH_LOAD_INTERVAL if high else SYSTEM_INTERVAL

            update_self_usage()

            if now - last_system_post >= interval:
                if send_webhook("system", SYSTEM_WEBHOOK_URL, system_embed(cpu, ram, alerts)):
                    last_system_post = now
            if battery is not None and now - last_battery_post >= BATTERY_INTERVAL:
                if send_webhook("battery", BATTERY_WEBHOOK_URL, battery_embed(battery, alerts)):
                    last_battery_post = now

            print(f"[*] CPU={'Sampling...' if cpu is None else f'{cpu:.1f}%'} "
                  f"RAM={'N/A' if ram is None else f'{ram['percent']:.1f}%'} "
                  f"BAT={'N/A' if battery is None else f'{battery.get('percentage', '?')}%'} "
                  f"SELF={monitor_cpu:.3f}%/{monitor_rss_mb:.2f}MB NEXT={interval}s")
        except KeyboardInterrupt:
            print("\n[+] Monitor stopped.")
            break
        except Exception as exc:
            print(f"[!] Recovered from: {exc}")

        elapsed = time.monotonic() - loop_start
        time.sleep(max(1.0, SYSTEM_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
