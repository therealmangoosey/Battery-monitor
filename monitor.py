import json
import os
import platform
import resource
import shutil
import subprocess
import time
from collections import deque

import requests

# Secrets are loaded from .env. Never put webhook URLs in this file.
SYSTEM_INTERVAL = 30
BATTERY_INTERVAL = 60
HIGH_INTERVAL = 60
EXTREME_INTERVAL = 120
HIGH_CPU, EXTREME_CPU = 50.0, 80.0
HIGH_RAM, EXTREME_RAM = 85.0, 95.0
LOW_BATTERY, CRITICAL_BATTERY = 15, 5
HIGH_TEMP, EXTREME_TEMP = 42.0, 48.0
HISTORY_SECONDS = 6 * 60 * 60
ALERT_COOLDOWN = 15 * 60


def load_env():
    # Deliberately tiny loader: no python-dotenv dependency required.
    try:
        with open('.env', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                value = value.strip().strip('"\'')
                os.environ.setdefault(key.strip(), value)
    except FileNotFoundError:
        pass


load_env()
BATTERY_WEBHOOK_URL = os.getenv('BATTERY_WEBHOOK_URL', '').strip()
SYSTEM_WEBHOOK_URL = os.getenv('SYSTEM_WEBHOOK_URL', '').strip()

http = requests.Session()
start = time.monotonic()
cpu_prev = None
cpu_value = None
cpu_samples = deque(maxlen=20)
ram_samples = deque(maxlen=20)
battery_history = deque(maxlen=16)
charge_history = deque(maxlen=16)
alerts_seen = {}
backoff = {'battery': 0.0, 'system': 0.0}
failures = {'battery': 0, 'system': 0}
last_system = last_battery_post = 0.0
last_battery = None
last_storage = last_thermal = last_device = 0.0
storage = None
thermal = []
device = {}
network = None
self_cpu = 0.0
self_rss = 0.0
self_cpu_prev = self_wall_prev = None


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def run(args, timeout=2):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def get_cpu():
    global cpu_prev, cpu_value
    try:
        with open('/proc/stat') as f:
            p = f.readline().split()
        if not p or p[0] != 'cpu':
            return cpu_value
        v = [int(x) for x in p[1:]]
        total, idle = sum(v), v[3] + (v[4] if len(v) > 4 else 0)
        if cpu_prev is None:
            cpu_prev = (total, idle)
            return None
        ot, oi = cpu_prev
        cpu_prev = (total, idle)
        dt, di = total - ot, idle - oi
        if dt <= 0:
            return cpu_value
        cpu_value = round(max(0, min(100, 100 * (1 - di / dt))), 1)
        cpu_samples.append(cpu_value)
        return cpu_value
    except Exception:
        return cpu_value


def get_ram():
    try:
        total = available = swap_total = swap_free = 0
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'): total = int(line.split()[1])
                elif line.startswith('MemAvailable:'): available = int(line.split()[1])
                elif line.startswith('SwapTotal:'): swap_total = int(line.split()[1])
                elif line.startswith('SwapFree:'): swap_free = int(line.split()[1])
        if not total: return None
        used = total - available
        pct = round(used * 100 / total, 1)
        ram_samples.append(pct)
        return {'percent': pct, 'used': round(used / 1024, 1),
                'total': round(total / 1024, 1),
                'swap_used': round((swap_total - swap_free) / 1024, 1),
                'swap_total': round(swap_total / 1024, 1)}
    except Exception:
        return None


def get_battery():
    try:
        p = subprocess.run(['termux-battery-status'], capture_output=True,
                           text=True, stderr=subprocess.DEVNULL, timeout=3)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None


def add_battery(b):
    pct = num(b.get('percentage')) if b else None
    if pct is None: return
    now = time.monotonic()
    charging = str(b.get('status', '')).lower() in ('charging', 'charged', 'full')
    h = charge_history if charging else battery_history
    other = battery_history if charging else charge_history
    h.append((now, pct)); other.clear()
    cutoff = now - HISTORY_SECONDS
    while h and h[0][0] < cutoff: h.popleft()


def slope(history):
    if len(history) < 2 or history[-1][0] - history[0][0] < 120: return None
    t0 = history[0][0]
    pts = [(t - t0, p) for t, p in history]
    xm = sum(x for x, _ in pts) / len(pts); ym = sum(y for _, y in pts) / len(pts)
    den = sum((x - xm) ** 2 for x, _ in pts)
    return None if den <= 0 else sum((x - xm) * (y - ym) for x, y in pts) / den


def fmt_eta(hours):
    if hours is None or hours < 0 or hours > 1000: return 'Unknown'
    h = int(hours); m = int((hours - h) * 60)
    return f'~{h}h {m}m'


def battery_estimate(b):
    if not b: return ('Collecting data...', 'Collecting data...', 'Low', 'Collecting data...', 'Collecting data...')
    pct = num(b.get('percentage'))
    charging = str(b.get('status', '')).lower() in ('charging', 'charged', 'full')
    if pct is None: return ('Unknown', 'Unknown', 'Low', 'Unknown', 'Unknown')
    s = slope(charge_history if charging else battery_history)
    if s is None:
        return ('Not discharging' if charging else 'Collecting data...',
                'Charging' if charging else 'Collecting data...', 'N/A' if charging else 'Low',
                'Collecting data...', 'Collecting data...')
    rate = s * 3600
    if charging and rate > 0:
        return ('Not discharging', 'Charging', 'N/A', fmt_eta((100-pct)/rate), f'+{rate:.2f}%/hour')
    if not charging and rate < 0:
        drain = -rate
        return (fmt_eta(pct/drain), f'{drain:.2f}%/hour',
                'High' if len(battery_history) >= 6 else 'Medium',
                'Not charging', 'Not charging')
    return ('Collecting data...', 'Collecting data...', 'Low', 'Collecting data...', 'Collecting data...')


def refresh_storage():
    try:
        total, used, free = shutil.disk_usage('/')
        return {'total': round(total/1024**3, 1), 'used': round(used/1024**3, 1),
                'free': round(free/1024**3, 1), 'percent': round(used*100/total, 1)}
    except Exception: return None


def refresh_thermal():
    out = []
    try:
        for z in os.listdir('/sys/class/thermal'):
            if not z.startswith('thermal_zone'): continue
            try:
                with open(f'/sys/class/thermal/{z}/temp') as f: t = float(f.read())
                if abs(t) > 200: t /= 1000
                with open(f'/sys/class/thermal/{z}/type') as f: name = f.read().strip()
                if -50 <= t <= 150: out.append({'type': name, 'temp': round(t, 1)})
            except Exception: pass
    except Exception: pass
    return out


def refresh_device():
    d = {'machine': platform.machine(), 'python': platform.python_version()}
    for k, prop in {'model':'ro.product.model','android':'ro.build.version.release',
                    'manufacturer':'ro.product.manufacturer','sdk':'ro.build.version.sdk'}.items():
        v = run(['getprop', prop])
        if v: d[k] = v
    return d


def get_network():
    try:
        with open('/proc/net/route') as f:
            return any(len(x:=line.split()) >= 4 and x[1] == '00000000' and int(x[3], 16) & 1 for line in f.readlines()[1:])
    except Exception: return None


def update_self():
    global self_cpu_prev, self_wall_prev, self_cpu, self_rss
    now = time.monotonic(); u = resource.getrusage(resource.RUSAGE_SELF)
    cpu = u.ru_utime + u.ru_stime
    if self_cpu_prev is not None and now > self_wall_prev:
        self_cpu = max(0.0, (cpu-self_cpu_prev)/(now-self_wall_prev)*100)
    self_cpu_prev, self_wall_prev = cpu, now
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'): self_rss = int(line.split()[1])/1024; break
    except Exception: pass


def alert(name):
    now = time.monotonic()
    if now - alerts_seen.get(name, 0) < ALERT_COOLDOWN: return False
    alerts_seen[name] = now; return True


def get_alerts(cpu, ram, b):
    a=[]
    if cpu is not None and cpu >= EXTREME_CPU and alert('cpu_extreme'): a.append(f'🚨 CPU extremely high: {cpu:.1f}%')
    elif cpu is not None and cpu >= HIGH_CPU and alert('cpu_high'): a.append(f'🔥 CPU high: {cpu:.1f}%')
    if ram and ram['percent'] >= EXTREME_RAM and alert('ram_extreme'): a.append(f'🚨 RAM extremely high: {ram["percent"]:.1f}%')
    elif ram and ram['percent'] >= HIGH_RAM and alert('ram_high'): a.append(f'🧠 RAM high: {ram["percent"]:.1f}%')
    if b:
        p=num(b.get('percentage')); t=num(b.get('temperature'))
        if p is not None and p <= CRITICAL_BATTERY and alert('battery_critical'): a.append(f'🚨 Battery critical: {p:.0f}%')
        elif p is not None and p <= LOW_BATTERY and alert('battery_low'): a.append(f'🪫 Battery low: {p:.0f}%')
        if t is not None and t >= EXTREME_TEMP and alert('temp_extreme'): a.append(f'🚨 Battery temperature: {t:.1f}°C')
        elif t is not None and t >= HIGH_TEMP and alert('temp_high'): a.append(f'🌡️ Battery temperature high: {t:.1f}°C')
    if thermal:
        z=max(thermal, key=lambda x:x['temp'])
        if z['temp'] >= 55 and alert('thermal'): a.append(f'🔥 Thermal zone high: {z["type"]} {z["temp"]:.1f}°C')
    return a


def post(kind, url, embed):
    if not url or time.monotonic() < backoff[kind]: return False
    try:
        r=http.post(url, json={'embeds':[embed]}, timeout=5)
        if r.status_code == 204:
            failures[kind]=0; backoff[kind]=0; return True
        if r.status_code == 429:
            try: retry=float(r.json().get('retry_after', 5))
            except Exception: retry=5
            backoff[kind]=time.monotonic()+min(retry,900); return False
        raise RuntimeError(f'HTTP {r.status_code}')
    except Exception as e:
        failures[kind]+=1; backoff[kind]=time.monotonic()+min(2**min(failures[kind],8),900)
        print(f'[!] {kind} webhook: {e}'); return False


def system_embed(cpu, ram, alerts):
    z=max(thermal,key=lambda x:x['temp']) if thermal else None
    fields=[
        {'name':'CPU','value':f'{cpu:.1f}%' if cpu is not None else 'Sampling...','inline':True},
        {'name':'CPU Avg','value':f'{sum(cpu_samples)/len(cpu_samples):.1f}%' if cpu_samples else 'N/A','inline':True},
        {'name':'CPU Peak','value':f'{max(cpu_samples):.1f}%' if cpu_samples else 'N/A','inline':True},
        {'name':'RAM','value':f'{ram["percent"]:.1f}% ({ram["used"]:.0f}/{ram["total"]:.0f} MB)' if ram else 'N/A','inline':True},
        {'name':'RAM Avg','value':f'{sum(ram_samples)/len(ram_samples):.1f}%' if ram_samples else 'N/A','inline':True},
        {'name':'Swap','value':f'{ram["swap_used"]:.0f}/{ram["swap_total"]:.0f} MB' if ram else 'N/A','inline':True},
        {'name':'Storage','value':f'{storage["used"]}/{storage["total"]} GB ({storage["percent"]}%)' if storage else 'N/A','inline':True},
        {'name':'Thermal','value':f'{z["type"]}: {z["temp"]:.1f}°C' if z else 'N/A','inline':True},
        {'name':'Network','value':'Online' if network is True else 'Offline' if network is False else 'Unknown','inline':True},
        {'name':'Monitor CPU','value':f'{self_cpu:.3f}%','inline':True},
        {'name':'Monitor RAM','value':f'{self_rss:.2f} MB','inline':True},
        {'name':'Device','value':f'{device.get("model","Android")} / Android {device.get("android","?")}','inline':True},
        {'name':'Uptime','value':str(time.monotonic()-start).split('.')[0]+'s','inline':True},
        {'name':'Updated','value':f'<t:{int(time.time())}:R>','inline':False},]
    e={'title':'💻 System Monitor','color':15158332 if cpu is not None and cpu>=HIGH_CPU else 3447003,'fields':fields,'footer':{'text':'Termux Monitor • Ultra Low Resource'}}
    if alerts: e['description']='\n'.join(alerts)
    return e


def battery_embed(b, alerts):
    eta_text, rate, confidence, ceta, crate = battery_estimate(b)
    v=num(b.get('voltage')); cur=num(b.get('current'))
    power=f'{abs(v*cur)/1_000_000:.2f} W' if v is not None and cur is not None else 'N/A'
    p=num(b.get('percentage')); color=15158332 if p is not None and p<=CRITICAL_BATTERY else 16776960 if p is not None and p<=LOW_BATTERY else 3066993
    e={'title':'🔋 Battery Monitor','color':color,'fields':[
        {'name':'Battery','value':f'**{b.get("percentage","?")}%**','inline':True},
        {'name':'Status','value':f'**{b.get("status","?")}**','inline':True},
        {'name':'Health','value':str(b.get('health','Unknown')),'inline':True},
        {'name':'Temperature','value':f'{b.get("temperature","?")} °C','inline':True},
        {'name':'Voltage','value':f'{b.get("voltage","?")} mV','inline':True},
        {'name':'Power','value':power,'inline':True},
        {'name':'Battery ETA','value':eta_text,'inline':True},
        {'name':'ETA Confidence','value':confidence,'inline':True},
        {'name':'Drain Rate','value':rate,'inline':True},
        {'name':'Charge Rate','value':crate,'inline':True},
        {'name':'Full Charge ETA','value':ceta,'inline':True},
        {'name':'Plugged','value':str(b.get('plugged',False)),'inline':True},
        {'name':'Updated','value':f'<t:{int(time.time())}:R>','inline':False}],
        'footer':{'text':'Termux Monitor • Smoothed Battery ETA'}}
    if alerts: e['description']='\n'.join(alerts)
    return e


def main():
    global last_system, last_battery_post, last_storage, last_thermal, last_device
    global last_battery, storage, thermal, device, network
    print('Starting Termux Monitor • Ultra Low Resource')
    if not BATTERY_WEBHOOK_URL and not SYSTEM_WEBHOOK_URL: print('[!] Configure .env first.')
    get_cpu()  # first CPU sample is intentionally unavailable
    device=refresh_device(); storage=refresh_storage(); thermal=refresh_thermal(); network=get_network()
    now=time.monotonic(); last_device=last_storage=last_thermal=now
    while True:
        loop=time.monotonic()
        try:
            cpu=get_cpu(); ram=get_ram(); now=time.monotonic()
            if now-last_device >= 1800: device=refresh_device(); last_device=now
            if now-last_storage >= 300: storage=refresh_storage(); network=get_network(); last_storage=now
            if now-last_thermal >= 300: thermal=refresh_thermal(); last_thermal=now
            if last_battery is None or now-last_battery_post >= BATTERY_INTERVAL:
                b=get_battery()
                if b: last_battery=b; add_battery(b)
            b=last_battery; alerts=get_alerts(cpu,ram,b)
            extreme=(cpu is not None and cpu>=EXTREME_CPU) or (ram and ram['percent']>=EXTREME_RAM)
            high=(cpu is not None and cpu>=HIGH_CPU) or (ram and ram['percent']>=HIGH_RAM) or (b and (num(b.get('temperature')) or 0)>=HIGH_TEMP)
            interval=EXTREME_INTERVAL if extreme else HIGH_INTERVAL if high else SYSTEM_INTERVAL
            update_self()
            if now-last_system >= interval and post('system',SYSTEM_WEBHOOK_URL,system_embed(cpu,ram,alerts)): last_system=now
            if b and now-last_battery_post >= BATTERY_INTERVAL and post('battery',BATTERY_WEBHOOK_URL,battery_embed(b,alerts)): last_battery_post=now
            print(f'[+] CPU={"Sampling..." if cpu is None else str(cpu)+"%"} RAM={"N/A" if not ram else str(ram["percent"])+"%"} BAT={"N/A" if not b else str(b.get("percentage","?"))+"%"} SELF={self_cpu:.3f}%/{self_rss:.2f}MB NEXT={interval}s')
        except KeyboardInterrupt:
            print('\n[+] Stopped.'); return
        except Exception as e:
            print(f'[!] Recovered: {e}')
        time.sleep(max(1.0, SYSTEM_INTERVAL-(time.monotonic()-loop)))


if __name__ == '__main__':
    main()
