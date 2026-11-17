"""System monitor: /proc + nvidia-smi parsers, the sampler and the /api/system endpoint."""
from __future__ import annotations

import pytest

STAT1 = "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 600 0 300 3800 300 0 0 0 0 0\ncpu1 400 0 200 4200 200 0 0 0 0 0\nintr 5\n"
STAT2 = "cpu  1600 0 700 8600 600 0 0 0 0 0\ncpu0 1100 0 400 3900 350 0 0 0 0 0\ncpu1 500 0 300 4700 250 0 0 0 0 0\nintr 9\n"
MEMINFO = ("MemTotal:       32000000 kB\nMemFree:         4000000 kB\nMemAvailable:   20000000 kB\nBuffers:          500000 kB\n"
           "Cached:         14000000 kB\nSReclaimable:     500000 kB\nSwapTotal:       8000000 kB\nSwapFree:        6000000 kB\n")
NETDEV = ("Inter-|   Receive                                                |  Transmit\n"
          " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
          "    lo: 9999999   100    0    0    0     0          0         0  9999999     100    0    0    0     0       0          0\n"
          "enp3s0: {rx}   200    0    0    0     0          0         0  {tx}     150    0    0    0     0       0          0\n"
          "tailscale0: {trx}   20    0    0    0     0          0         0  {ttx}     15    0    0    0     0       0          0\n"
          "docker0: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\nveth12ab: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n")
DISK = (" 259       0 nvme0n1 100 0 {r} 50 200 0 {w} 80 0 {busy} 130 0 0 0 0\n"
        " 259       1 nvme0n1p1 90 0 9999 40 190 0 9999 70 0 100 110 0 0 0 0\n"
        "   7       0 loop0 10 0 9999 5 0 0 0 0 0 5 5 0 0 0 0\n   8       0 sda 1 0 {r} 1 1 0 {w} 1 0 0 2 0 0 0 0\n   8       1 sda1 1 0 8 1 1 0 8 1 0 1 2\n")
GPU = "NVIDIA GeForce RTX 2070 with Max-Q Design, 87, 6144, 8192, 71, 78.52, 40, [N/A]\n"


def test_cpu_parsing_and_percent():
    from app import sysmon
    a, b = sysmon.parse_stat(STAT1), sysmon.parse_stat(STAT2)
    assert set(a) == {"cpu", "cpu0", "cpu1"} and a["cpu"] == (1500, 10000)              # idle + iowait are not "busy"
    assert sysmon.cpu_percent(a["cpu"], b["cpu"]) == 53.3                                 # 800 busy of 1500 jiffies
    assert sysmon.cpu_percent(a["cpu0"], b["cpu0"]) == 80.0 and sysmon.cpu_percent(a["cpu1"], b["cpu1"]) == 26.7
    assert sysmon.cpu_percent(None, b["cpu"]) == 0.0 and sysmon.cpu_percent(b["cpu"], b["cpu"]) == 0.0


def test_memory_parsing():
    from app import sysmon
    m = sysmon.parse_meminfo(MEMINFO)
    assert (m["total_mb"], m["used_mb"], m["available_mb"]) == (31250, 11719, 19531) and m["pct"] == 37.5
    assert m["cached_mb"] == 14648 and (m["swap_total_mb"], m["swap_used_mb"]) == (7812, 1953)
    assert sysmon.parse_meminfo("")["pct"] == 0.0


def test_network_and_disk_parsing():
    from app import sysmon
    n = sysmon.parse_netdev(NETDEV.format(rx=1000, tx=2000, trx=10, ttx=20))
    assert n == {"enp3s0": (1000, 2000), "tailscale0": (10, 20)}                         # loopback, docker and veth are left out
    d = sysmon.parse_diskstats(DISK.format(r=2000, w=4000, busy=500))
    assert d == {"nvme0n1": (2000 * 512, 4000 * 512, 500), "sda": (2000 * 512, 4000 * 512, 0)}   # whole disks only


def test_gpu_parsing():
    from app import sysmon
    g = sysmon.parse_gpu(GPU)
    assert g == {"ok": True, "name": "NVIDIA GeForce RTX 2070 with Max-Q Design", "util": 87.0, "mem_used_mb": 6144,
                 "mem_total_mb": 8192, "mem_pct": 75.0, "temp_c": 71.0, "power_w": 78.52, "mem_util": 40.0, "fan_pct": None}
    assert sysmon.parse_gpu("")["ok"] is False and sysmon.parse_gpu("garbage")["ok"] is False
    assert sysmon.parse_gpu("RTX, [N/A], 1, 2, 3")["ok"] is False
    assert sysmon.parse_gpu("RTX 2070, 5, 100, 8192, 40")["power_w"] is None             # older drivers: fewer columns
    apps = sysmon.parse_gpu_apps("2100, /home/u/yt/.venv/bin/python, 3900\n1422, /usr/local/bin/ollama, 5120\nbad line\n")
    assert apps == [{"pid": 1422, "name": "ollama", "mem_mb": 5120}, {"pid": 2100, "name": "python", "mem_mb": 3900}]
    assert sysmon.parse_gpu_apps("") == []


def test_secrets_are_redacted_from_command_lines():
    from app.sysmon import redact
    assert redact("n8n start --api-key=abc123 --port 5678") == "n8n start --api-key=*** --port 5678"
    assert redact("env YTM_API_KEY=s3cret python -m app.server") == "env YTM_API_KEY=*** python -m app.server"
    assert redact("curl -H token: xyz") == "curl -H token: ***" and redact("mysql --password hunter2") == "mysql --password ***"
    assert redact("python -m app.server") == "python -m app.server"


@pytest.fixture
def fake_host(env, monkeypatch):
    """A machine whose counters we control: two samples 2 s apart."""
    from app import sysmon
    st = {"stat": STAT1, "rx": 1_000_000, "tx": 500_000, "r": 2000, "w": 4000, "busy": 500, "gpu": GPU, "apps": "7, /x/python, 2048\n"}

    def read(path):
        p = str(path)
        if p == "/proc/stat":
            return st["stat"]
        if p == "/proc/meminfo":
            return MEMINFO
        if p == "/proc/net/dev":
            return NETDEV.format(rx=st["rx"], tx=st["tx"], trx=0, ttx=0)
        if p == "/proc/diskstats":
            return DISK.format(r=st["r"], w=st["w"], busy=st["busy"])
        if p == "/proc/uptime":
            return "93784.5 1.0\n"
        return ""
    monkeypatch.setattr(sysmon, "_read", read)
    monkeypatch.setattr(sysmon, "_smi", lambda args: (st["gpu"] if "query-gpu" in args[0] else st["apps"]))
    monkeypatch.setattr(sysmon, "top_processes", lambda prev, total, n=6: ([{"pid": 1, "name": "python", "cmd": "python -m app.server", "cpu": 12.5, "mem_mb": 900}], {}))
    monkeypatch.setattr(sysmon, "cpu_temp", lambda: 66.0)
    monkeypatch.setattr(sysmon, "cpu_mhz", lambda: 3200)
    monkeypatch.setattr(sysmon, "_monitor", None)
    return st


def test_sampler_turns_counters_into_rates(fake_host):
    from app import sysmon
    m = sysmon.Monitor()
    first = m.sample(now=1000.0)
    assert first["cpu"]["pct"] == 0.0 and first["net"]["rx"] == 0 and first["disk"]["read"] == 0   # no previous sample yet
    fake_host.update(stat=STAT2, rx=1_000_000 + 2_500_000, tx=500_000 + 250_000, r=2000 + 4000, w=4000 + 20000, busy=500 + 1000)
    s = m.sample(now=1002.0)
    assert s["cpu"] == {"pct": 53.3, "cores": [80.0, 26.7], "mhz": 3200, "temp_c": 66.0, "load1": s["cpu"]["load1"]}
    assert s["net"]["ifaces"]["enp3s0"] == {"rx": 1_250_000, "tx": 125_000} and s["net"]["rx"] == 1_250_000   # bytes per second
    assert s["disk"]["read"] == 2 * 4000 * 512 // 2 and s["disk"]["write"] == 2 * 20000 * 512 // 2            # two disks moved
    assert s["disk"]["busy"] == 50.0                                                      # 1000 ms busy in 2 s
    assert s["gpu"]["util"] == 87.0 and s["mem"]["pct"] == 37.5
    assert m.gpu_apps == [{"pid": 7, "name": "python", "mem_mb": 2048}] and m.procs[0]["name"] == "python"
    # a counter reset (reboot of an interface) must never produce a negative or huge rate
    fake_host.update(rx=10, tx=10)
    assert m.sample(now=1004.0)["net"]["rx"] == 0


def test_history_is_bounded_and_incremental(fake_host, env):
    from app import sysmon
    env.set(monitor__interval_s=2, monitor__history_s=60)
    m = sysmon.Monitor()
    assert m.samples.maxlen == 30
    for i in range(40):
        m.sample(now=2000.0 + 2 * i)
    snap = m.snapshot()
    assert len(snap["samples"]) == 30 and snap["samples"][0]["t"] == 2020.0 and snap["interval"] == 2.0
    assert len(m.snapshot(since=2070.0)["samples"]) == 4                                  # only what the browser has not seen
    assert snap["info"]["uptime_s"] == 93784 and snap["info"]["hostname"]


def test_missing_gpu_and_broken_source_do_not_break_sampling(fake_host, monkeypatch):
    from app import sysmon
    monkeypatch.setattr(sysmon, "_smi", lambda args: None)
    m = sysmon.Monitor()
    s = m.sample(now=1.0)
    assert s["gpu"] == {"ok": False, "error": "nvidia-smi not found"} and m.gpu_apps == [] and s["cpu"]["cores"] == [0.0, 0.0]
    fake_host["gpu"] = "nonsense"
    monkeypatch.setattr(sysmon, "_smi", lambda args: "nonsense")
    assert m.sample(now=3.0)["gpu"]["ok"] is False


def test_real_machine_sample_has_sane_values(env, monkeypatch):
    """No fakes: read this machine's real /proc."""
    import time
    from app import sysmon
    monkeypatch.setattr(sysmon, "_monitor", None)
    m = sysmon.Monitor()
    m.sample()
    sum(i * i for i in range(400_000))
    time.sleep(0.3)
    s = m.sample()
    assert 0 <= s["cpu"]["pct"] <= 100 and len(s["cpu"]["cores"]) == m.info["cpu_threads"] >= 1
    assert s["mem"]["total_mb"] > 500 and 0 < s["mem"]["pct"] < 100 and s["disk"]["total_gb"] > 0
    assert s["net"]["rx"] >= 0 and isinstance(s["net"]["ifaces"], dict) and m.info["cpu_model"]
    assert any(p["mem_mb"] > 0 for p in m.procs) and all("***" in p["cmd"] or "KEY=" not in p["cmd"].upper() for p in m.procs)


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app import server
    monkeypatch.delenv("YTM_API_KEY", raising=False)
    monkeypatch.setattr(server.worker, "start", lambda: None)
    with TestClient(server.app) as c:
        yield c


def test_api_system_endpoint(fake_host, client, env):
    r = client.get("/api/system")
    assert r.status_code == 200
    d = r.json()
    assert d["samples"] and d["info"]["cpu_threads"] >= 1 and d["job"] is None and d["processes"][0]["cmd"] == "python -m app.server"
    assert {"cpu", "mem", "net", "disk", "gpu", "t"} <= set(d["samples"][-1])
    newest = d["samples"][-1]["t"]
    assert client.get(f"/api/system?since={newest + 1000}").json()["samples"] == []
    env.set(monitor__enabled=False)
    assert client.get("/api/system").status_code == 404


def test_api_system_requires_key(fake_host, client, monkeypatch):
    monkeypatch.setenv("YTM_API_KEY", "secret")
    assert client.get("/api/system").status_code == 401
    assert client.get("/api/system", headers={"X-API-Key": "secret"}).status_code == 200
