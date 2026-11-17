"""System monitor: CPU, memory, GPU, network and disk samples for the dashboard's System tab
(a Task Manager-style performance view).

A small background thread takes a sample every `monitor.interval_s` seconds and keeps the last
`monitor.history_s` seconds in memory. Everything is read straight from /proc and nvidia-smi:
no extra packages, a few milliseconds of CPU per sample. Nothing is written to disk.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .config import data_dir, settings

log = logging.getLogger("ytm.sysmon")
PROC = Path("/proc")
GPU_FIELDS = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,utilization.memory,fan.speed"
VIRTUAL_NICS = ("lo", "veth", "docker", "br-", "virbr", "vnet", "tap", "dummy", "ifb", "bond", "sit", "ip6tnl")


# --------------------------------------------------------------------------- parsers (pure: text in, numbers out)
def parse_stat(text: str) -> dict[str, tuple[int, int]]:
    """/proc/stat -> {"cpu": (busy, total), "cpu0": ...} in jiffies."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        p = line.split()
        v = [int(x) for x in p[1:9]] + [0] * (8 - len(p[1:9]))
        idle = v[3] + v[4]                              # idle + iowait
        out[p[0]] = (sum(v) - idle, sum(v))
    return out


def cpu_percent(prev: tuple[int, int] | None, cur: tuple[int, int]) -> float:
    if not prev or cur[1] <= prev[1]:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]))), 1)


def parse_meminfo(text: str) -> dict:
    kb = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        try:
            kb[k] = int(rest.split()[0])
        except (IndexError, ValueError):
            pass
    total = kb.get("MemTotal", 0)
    avail = kb.get("MemAvailable", kb.get("MemFree", 0))
    cached = kb.get("Cached", 0) + kb.get("Buffers", 0) + kb.get("SReclaimable", 0)
    mb = lambda v: round(v / 1024)                     # noqa: E731
    return {"total_mb": mb(total), "used_mb": mb(total - avail), "available_mb": mb(avail), "cached_mb": mb(cached),
            "free_mb": mb(kb.get("MemFree", 0)), "swap_total_mb": mb(kb.get("SwapTotal", 0)),
            "swap_used_mb": mb(kb.get("SwapTotal", 0) - kb.get("SwapFree", 0)),
            "pct": round(100.0 * (total - avail) / total, 1) if total else 0.0}


def parse_netdev(text: str) -> dict[str, tuple[int, int]]:
    """/proc/net/dev -> {iface: (rx_bytes, tx_bytes)} without loopback and virtual bridges."""
    out = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name, f = name.strip(), rest.split()
        if len(f) < 9 or name.startswith(VIRTUAL_NICS):
            continue
        out[name] = (int(f[0]), int(f[8]))
    return out


def parse_diskstats(text: str) -> dict[str, tuple[int, int, int]]:
    """/proc/diskstats -> {disk: (read_bytes, written_bytes, busy_ms)} for whole disks only."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if name.startswith(("loop", "ram", "dm-", "zram", "sr", "fd")):
            continue
        whole = (name.startswith("nvme") and "p" not in name[4:]) or (name.startswith("mmcblk") and "p" not in name[6:]) \
            or (name[:2] in ("sd", "vd", "hd") or name[:3] == "xvd") and not name[-1].isdigit()
        if whole:
            out[name] = (int(f[5]) * 512, int(f[9]) * 512, int(f[12]))
    return out


def _num(x: str):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None                                     # "[N/A]" on laptops for fan / power


def parse_gpu(csv: str) -> dict:
    """First line of `nvidia-smi --query-gpu=<GPU_FIELDS> --format=csv,noheader,nounits`."""
    line = (csv or "").strip().splitlines()[0] if (csv or "").strip() else ""
    p = [x.strip() for x in line.split(",")]
    if len(p) < 5:
        return {"ok": False, "error": "unexpected nvidia-smi output"}
    util, used, total, temp = _num(p[1]), _num(p[2]), _num(p[3]), _num(p[4])
    if util is None or used is None or not total:
        return {"ok": False, "error": "unexpected nvidia-smi output"}
    g = {"ok": True, "name": p[0], "util": util, "mem_used_mb": int(used), "mem_total_mb": int(total),
         "mem_pct": round(100.0 * used / total, 1), "temp_c": temp}
    for key, i in (("power_w", 5), ("mem_util", 6), ("fan_pct", 7)):
        g[key] = _num(p[i]) if len(p) > i else None
    return g


def parse_gpu_apps(csv: str) -> list[dict]:
    """`nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits`."""
    out = []
    for line in (csv or "").strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and p[0].isdigit():
            out.append({"pid": int(p[0]), "name": Path(p[1]).name or p[1], "mem_mb": int(_num(p[2]) or 0)})
    return sorted(out, key=lambda a: -a["mem_mb"])


# --------------------------------------------------------------------------- readers
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _smi(args: list[str]) -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *args, "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def cpu_temp() -> float | None:
    """CPU package temperature from hwmon (coretemp on Intel, k10temp/zenpower on AMD), if exposed."""
    best = None
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        name = _read(hw / "name").strip()
        if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            continue
        for f in sorted(hw.glob("temp*_input")):
            try:
                t = int(_read(f).strip()) / 1000.0
            except ValueError:
                continue
            label = _read(f.with_name(f.name.replace("_input", "_label"))).lower()
            if "package" in label or "tctl" in label:
                return round(t, 1)
            best = t if best is None else max(best, t)
    return round(best, 1) if best is not None else None


def cpu_mhz() -> float | None:
    vals = [float(l.split(":")[1]) for l in _read(PROC / "cpuinfo").splitlines() if l.startswith("cpu MHz")]
    return round(sum(vals) / len(vals)) if vals else None


def static_info() -> dict:
    model, cores = "", set()
    phys = core = None
    threads = 0
    for line in _read(PROC / "cpuinfo").splitlines():
        k, _, val = line.partition(":")
        k, val = k.strip(), val.strip()
        if k == "model name" and not model:
            model = val
        elif k == "processor":
            threads += 1
        elif k == "physical id":
            phys = val
        elif k == "core id":
            core = val
            cores.add((phys, core))
    os_name = ""
    for line in _read(Path("/etc/os-release")).splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"')
    return {"hostname": socket.gethostname(), "os": os_name or platform.system(), "kernel": platform.release(),
            "cpu_model": " ".join(model.replace("(R)", "").replace("(TM)", "").split()) or platform.processor() or "CPU",
            "cpu_threads": threads or os.cpu_count() or 1, "cpu_cores": len(cores) or None}


def redact(cmd: str) -> str:
    """Command lines can carry secrets (--api-key=..., TOKEN=...): never send those to the browser."""
    import re
    return re.sub(r"(?i)((?:key|token|secret|passw(?:or)?d|auth)[\w-]*[=: ]\s*)\S+", r"\1***", cmd)


def top_processes(prev: dict, ticks_total: int, n: int = 6) -> tuple[list[dict], dict]:
    """Busiest processes since the last call. prev/returned: {pid: cpu_jiffies}."""
    page_mb = os.sysconf("SC_PAGE_SIZE") / 2**20
    cur, rows = {}, []
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        s = _read(d / "stat")
        r = s.rfind(")")
        if r < 0:
            continue
        f = s[r + 2:].split()
        try:
            jiff, rss = int(f[11]) + int(f[12]), int(f[21])
        except (IndexError, ValueError):
            continue
        pid = int(d.name)
        cur[pid] = jiff
        if rss <= 0:
            continue                                    # kernel threads
        cpu = 100.0 * (jiff - prev[pid]) / ticks_total if pid in prev and ticks_total > 0 else 0.0
        rows.append((cpu, rss * page_mb, pid, s[s.find("(") + 1:r]))
    pick = {r[2]: r for r in sorted(rows, key=lambda r: -r[0])[:n]}
    pick.update({r[2]: r for r in sorted(rows, key=lambda r: -r[1])[:n]})
    out = []
    for cpu, mem, pid, name in pick.values():
        cmd = redact(_read(PROC / str(pid) / "cmdline").replace("\x00", " ").strip())
        out.append({"pid": pid, "name": name, "cmd": cmd[:90], "cpu": round(cpu, 1), "mem_mb": round(mem)})
    return sorted(out, key=lambda p: (-p["cpu"], -p["mem_mb"])), cur


# --------------------------------------------------------------------------- sampler
class Monitor:
    def __init__(self):
        self.interval = max(1.0, float(settings.get("monitor.interval_s", 2)))
        self.samples: deque = deque(maxlen=max(30, int(float(settings.get("monitor.history_s", 600)) / self.interval)))
        self.info = static_info()
        self.procs: list[dict] = []
        self.gpu_apps: list[dict] = []
        self._prev: dict = {}
        self._proc_prev: dict = {}
        self._proc_total = 0
        self._n = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # one sample --------------------------------------------------------
    def sample(self, now: float | None = None) -> dict:
        now = now or time.time()
        prev, dt = self._prev, 0.0
        if prev:
            dt = max(1e-3, now - prev["t"])
        stat = parse_stat(_read(PROC / "stat"))
        net = parse_netdev(_read(PROC / "net" / "dev"))
        disk = parse_diskstats(_read(PROC / "diskstats"))
        s = {"t": round(now, 2)}
        p_stat = prev.get("stat", {})
        cores = sorted((k for k in stat if k != "cpu"), key=lambda k: int(k[3:]))
        s["cpu"] = {"pct": cpu_percent(p_stat.get("cpu"), stat.get("cpu", (0, 0))),
                    "cores": [cpu_percent(p_stat.get(k), stat[k]) for k in cores],
                    "mhz": cpu_mhz(), "temp_c": cpu_temp(), "load1": round(os.getloadavg()[0], 2)}
        s["mem"] = parse_meminfo(_read(PROC / "meminfo"))
        rate = lambda cur, old: round(max(0, cur - old) / dt) if dt else 0   # noqa: E731
        ifaces = {}
        for name, (rx, tx) in net.items():
            o = prev.get("net", {}).get(name)
            ifaces[name] = {"rx": rate(rx, o[0]) if o else 0, "tx": rate(tx, o[1]) if o else 0}
        s["net"] = {"rx": sum(i["rx"] for i in ifaces.values()), "tx": sum(i["tx"] for i in ifaces.values()), "ifaces": ifaces}
        rd = wr = busy = 0
        for name, (r, w, b) in disk.items():
            o = prev.get("disk", {}).get(name)
            if o:
                rd, wr = rd + rate(r, o[0]), wr + rate(w, o[1])
                busy = max(busy, min(100.0, (b - o[2]) / (dt * 10.0)) if dt else 0)
        try:
            du = shutil.disk_usage(data_dir())
            usage = {"total_gb": round(du.total / 1e9, 1), "used_gb": round(du.used / 1e9, 1), "free_gb": round(du.free / 1e9, 1),
                     "pct": round(100.0 * du.used / du.total, 1) if du.total else 0}
        except OSError:
            usage = {}
        s["disk"] = {"read": rd, "write": wr, "busy": round(busy, 1), **usage}
        raw = _smi([f"--query-gpu={GPU_FIELDS}"])
        s["gpu"] = parse_gpu(raw) if raw is not None else {"ok": False, "error": "nvidia-smi not found"}
        if self._n % 3 == 0:                            # heavier lists every third sample
            total = stat.get("cpu", (0, 0))[1]
            self.procs, self._proc_prev = top_processes(self._proc_prev, total - self._proc_total if self._proc_total else 0)
            self._proc_total = total
            apps = _smi(["--query-compute-apps=pid,process_name,used_memory"])
            self.gpu_apps = parse_gpu_apps(apps) if apps else []
        self._n += 1
        self._prev = {"t": now, "stat": stat, "net": net, "disk": disk}
        with self._lock:
            self.samples.append(s)
        return s

    # thread ------------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.sample(t0)
            except Exception as e:  # noqa: BLE001 - monitoring must never take the API down
                log.warning("sample failed: %s", e)
            self._stop.wait(max(0.2, self.interval - (time.time() - t0)))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ytm-sysmon", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self, since: float = 0.0, limit: int = 600) -> dict:
        with self._lock:
            rows = [s for s in self.samples if s["t"] > since][-limit:]
        up = _read(PROC / "uptime").split()
        return {"interval": self.interval, "history_s": self.samples.maxlen * self.interval, "now": round(time.time(), 2),
                "info": {**self.info, "uptime_s": int(float(up[0])) if up else None},
                "samples": rows, "processes": self.procs, "gpu_processes": self.gpu_apps}


_monitor: Monitor | None = None


def monitor() -> Monitor:
    global _monitor
    if _monitor is None:
        _monitor = Monitor()
    return _monitor


def start() -> None:
    if settings.get("monitor.enabled", True):
        monitor().start()


def stop() -> None:
    if _monitor:
        _monitor.stop()
