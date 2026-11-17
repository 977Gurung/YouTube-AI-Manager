"""Idle memory (production jobs run in their own process) and the System tab's power controls."""
from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT
from test_agents import scripted


def _child_env(env) -> dict:
    """What the real service gives a job process: same config dir; in tests also the fake web."""
    return {**os.environ, "YTM_CONFIG_DIR": str(env.cfg_dir), "YTM_FAKEWEB_PORT": str(env.web.port),
            "YTM_ROOT_FOR_TESTS": str(ROOT), "PYTHONPATH": os.pathsep.join([str(ROOT / "tests" / "live"), str(ROOT)])}


# =========================================================================== memory
def test_isolated_produce_keeps_the_service_process_light(env):
    """Same produce job run the old way (inside the service) and the new way (own process): with
    isolation the service never even loads PyTorch/diffusers, so its memory stays small."""
    a, b = scripted("short"), scripted("short")
    out = {}
    for vid, iso in ((a, "0"), (b, "1")):
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "tools" / "measure_worker_memory.py"), str(vid), iso],
                           capture_output=True, text=True, env=_child_env(env), cwd=str(ROOT), timeout=1200)
        assert p.returncode == 0, p.stderr[-2000:]
        out[iso] = json.loads(p.stdout.strip().splitlines()[-1])
    old, new = out["0"], out["1"]
    print("service process after one produce job - in-process:", old, " isolated:", new)
    assert old["job"] == new["job"] == "done" and old["video"] == new["video"] == "final_review"
    assert old["torch_loaded"] and old["diffusers_loaded"]
    assert not new["torch_loaded"] and not new["diffusers_loaded"]
    assert new["rss_mb"] < old["rss_mb"] / 2 and new["rss_mb"] < 400


def test_isolated_job_failure_is_recorded_like_before(env, monkeypatch):
    from app import db, pipeline, worker as wmod
    for k, v in _child_env(env).items():
        monkeypatch.setenv(k, v)
    env.set(worker__isolate=["produce"])
    vid = scripted("short")
    env.set_channel("tech-simplified", voice={"provider": "elevenlabs"})     # no ELEVENLABS_API_KEY -> voice step fails
    pipeline.enqueue("produce", vid)
    w = wmod.Worker()
    w.start()
    t = time.time()
    while time.time() - t < 300 and db.get_video(vid)["status"] not in ("failed", "final_review"):
        time.sleep(0.3)
    w.stop()
    w.join(timeout=10)
    job = db.one("SELECT status, error FROM jobs WHERE kind='produce'")
    v = db.get_video(vid)
    assert job["status"] == "failed" and "ELEVENLABS" in job["error"].upper()
    assert v["status"] == "failed" and v["resume_status"] == "script_approved" and "produce:" in v["error"]


def test_killed_job_process_explains_itself(env, monkeypatch):
    from app import worker as wmod
    monkeypatch.setattr(wmod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, -9))
    with pytest.raises(wmod.JobFailed, match="killed - most likely out of memory"):
        wmod.run_isolated({"id": 1, "kind": "produce"})
    monkeypatch.setattr(wmod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 3))
    with pytest.raises(wmod.JobFailed, match="exit code 3"):
        wmod.run_isolated({"id": 1, "kind": "produce"})


def test_only_configured_kinds_are_isolated(env):
    from app import worker as wmod
    assert wmod.isolated("produce") and not wmod.isolated("script") and not wmod.isolated("upload")
    env.set(worker__isolate=[])
    assert not wmod.isolated("produce")
    wmod.release_memory()                                                # never raises


def test_jobrun_entry_point(env, monkeypatch, tmp_path):
    from app import jobrun, pipeline
    out = tmp_path / "r.json"
    assert jobrun.main(["999", str(out)]) == 1 and "not found" in json.loads(out.read_text())["error"]
    jid = pipeline.enqueue("analytics")
    monkeypatch.setattr(pipeline, "run_job", lambda job: {"echo": job["kind"]})
    assert jobrun.main([str(jid), str(out)]) == 0 and json.loads(out.read_text()) == {"ok": True, "result": {"echo": "analytics"}}
    monkeypatch.setattr(pipeline, "run_job", lambda job: (_ for _ in ()).throw(RuntimeError("boom")))
    assert jobrun.main([str(jid), str(out)]) == 1
    r = json.loads(out.read_text())
    assert r["ok"] is False and r["error"] == "boom" and "RuntimeError" in r["traceback"]


# =========================================================================== power: API
@pytest.fixture
def fake_sudo(env, monkeypatch, tmp_path):
    """A `sudo` on PATH that allows whatever is listed in $ALLOW and logs real runs."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "sudo.log"
    (bin_ / "sudo").write_text(f"""#!/bin/bash
[[ "$1" == "-n" ]] && shift
if [[ "$1" == "-l" ]]; then shift; [[ " $ALLOW " == *" $3 "* ]] && exit 0 || exit 1; fi
echo "$@" >> '{log}'
""")
    (bin_ / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ALLOW", "reboot poweroff restart")
    from app import power
    power._cache.update(t=0.0, value=None)
    return log


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app import server
    monkeypatch.delenv("YTM_API_KEY", raising=False)
    monkeypatch.setattr(server.worker, "start", lambda: None)
    monkeypatch.setattr(server.worker, "current", None)
    with TestClient(server.app) as c:
        yield c


def test_power_status_reflects_sudo_rule(fake_sudo, client, monkeypatch):
    from app import power
    st = client.get("/api/system/power").json()
    assert st["available"] and st["actions"] == ["reboot", "shutdown", "restart-app"] and st["job"] is None
    monkeypatch.setenv("ALLOW", "")
    power._cache.update(t=0.0, value=None)
    st = client.get("/api/system/power").json()
    assert not st["available"] and "setup-power.sh" in st["reason"]


def test_power_action_guards_and_runs(fake_sudo, client, monkeypatch):
    from app import db, server
    assert client.post("/api/system/power", json={"action": "format-disk", "confirm": "format-disk"}).status_code == 400
    assert client.post("/api/system/power", json={"action": "reboot"}).status_code == 400              # no confirm
    assert client.post("/api/system/power", json={"action": "reboot", "confirm": "shutdown"}).status_code == 400
    monkeypatch.setattr(server.worker, "current", {"kind": "produce", "video_id": 7})
    r = client.post("/api/system/power", json={"action": "reboot", "confirm": "reboot"})
    assert r.status_code == 409 and "video #7" in r.json()["detail"]
    assert not fake_sudo.exists()                                                                     # nothing ran
    r = client.post("/api/system/power", json={"action": "reboot", "confirm": "reboot", "force": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "action": "reboot", "in_seconds": 2}
    time.sleep(3)
    assert fake_sudo.read_text().splitlines() == ["/usr/bin/systemctl --no-block reboot"]
    ev = db.q("SELECT actor, action, detail FROM events WHERE action LIKE 'power_%'")
    assert ev == [{"actor": "dashboard", "action": "power_reboot", "detail": "requested from the dashboard while produce was running"}]
    monkeypatch.setattr(server.worker, "current", None)
    client.post("/api/system/power", json={"action": "restart-app", "confirm": "restart-app"})
    client.post("/api/system/power", json={"action": "shutdown", "confirm": "shutdown"})
    time.sleep(3)
    assert fake_sudo.read_text().splitlines()[1:] == ["/usr/bin/systemctl --no-block restart ytm-api.service ytm-n8n.service",
                                                     "/usr/bin/systemctl --no-block poweroff"] or \
           sorted(fake_sudo.read_text().splitlines()[1:]) == sorted(["/usr/bin/systemctl --no-block restart ytm-api.service ytm-n8n.service",
                                                                     "/usr/bin/systemctl --no-block poweroff"])


def test_power_refused_when_not_set_up_or_disabled(fake_sudo, client, monkeypatch, env):
    from app import power
    monkeypatch.setenv("ALLOW", "reboot")
    power._cache.update(t=0.0, value=None)
    assert client.post("/api/system/power", json={"action": "shutdown", "confirm": "shutdown"}).status_code == 403
    env.set(power__enabled=False)
    power._cache.update(t=0.0, value=None)
    r = client.post("/api/system/power", json={"action": "reboot", "confirm": "reboot"})
    assert r.status_code == 403 and "power.enabled" in r.json()["detail"]


def test_power_needs_api_key(fake_sudo, client, monkeypatch):
    monkeypatch.setenv("YTM_API_KEY", "secret")
    assert client.post("/api/system/power", json={"action": "reboot", "confirm": "reboot"}).status_code == 401
    assert client.get("/api/system/power").status_code == 401


# =========================================================================== power: the real sudo rule
@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("visudo") or not shutil.which("useradd"), reason="needs root, sudo, visudo")
def test_setup_power_rule_with_real_sudo(tmp_path):
    """Install the shipped rule with the shipped script, then ask REAL sudo, as a normal user, what
    that user may run: exactly the three commands, nothing else, no password."""
    user = "ytmpowertest"
    if subprocess.run(["id", user], capture_output=True).returncode != 0:
        subprocess.run(["useradd", "-M", "-s", "/bin/bash", user], check=True)
    dest = Path("/etc/sudoers.d/ytm-power-test")
    e = {**os.environ, "YTM_SERVICE_USER": user, "YTM_SUDOERS_DEST": str(dest)}
    try:
        r = subprocess.run(["bash", str(ROOT / "scripts" / "setup-power.sh")], capture_output=True, text=True, env=e, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        st = dest.stat()
        assert stat.S_IMODE(st.st_mode) == 0o440 and st.st_uid == 0 and user in dest.read_text()
        assert subprocess.run(["visudo", "-c"], capture_output=True).returncode == 0                  # whole sudo config still valid

        def allowed(*cmd):
            return subprocess.run(["runuser", "-u", user, "--", "sudo", "-n", "-l", *cmd], capture_output=True).returncode == 0
        assert allowed("/usr/bin/systemctl", "--no-block", "reboot")
        assert allowed("/usr/bin/systemctl", "--no-block", "poweroff")
        assert allowed("/usr/bin/systemctl", "--no-block", "restart", "ytm-api.service", "ytm-n8n.service")
        assert not allowed("/usr/bin/systemctl", "--no-block", "restart", "ssh.service")
        assert not allowed("/usr/bin/systemctl", "reboot", "--force")
        assert not allowed("/usr/bin/systemctl", "--no-block", "reboot", "now")
        assert not allowed("/bin/bash")
        assert not allowed("/usr/bin/systemctl", "edit", "ytm-api.service")
        r = subprocess.run(["bash", str(ROOT / "scripts" / "setup-power.sh"), "--remove"], capture_output=True, text=True, env=e)
        assert r.returncode == 0 and not dest.exists() and not allowed("/usr/bin/systemctl", "--no-block", "reboot")
    finally:
        dest.unlink(missing_ok=True)


def test_setup_power_refuses_a_broken_rule(tmp_path):
    if not shutil.which("visudo"):
        pytest.skip("needs visudo")
    proj = tmp_path / "p"
    (proj / "scripts").mkdir(parents=True)
    (proj / "deploy" / "sudoers").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "setup-power.sh", proj / "scripts")
    (proj / "deploy" / "sudoers" / "ytm-power").write_text("@USER@ ALL=(root NOPASSWD /usr/bin/x\n")   # syntax error
    dest = tmp_path / "out"
    r = subprocess.run(["bash", str(proj / "scripts" / "setup-power.sh")], capture_output=True, text=True,
                       env={**os.environ, "YTM_SUDOERS_DEST": str(dest)})
    assert r.returncode == 1 and "did not validate" in r.stdout and not dest.exists()
