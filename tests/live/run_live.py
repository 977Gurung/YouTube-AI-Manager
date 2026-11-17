"""Live sandbox run: real API server process + real Chromium clicking the dashboard + real n8n
executing the five shipped workflows against it. Outside services are FakeWeb.

Usage: python tests/live/run_live.py <workdir> [n8n_bin_dir]
Writes <workdir>/results.json and screenshots to <workdir>/screens/."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from conftest import CHANNELS, SHIPPED_SETTINGS  # noqa: E402
from fakes import FakeWeb, make_media  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
N8N_BIN = Path(sys.argv[2]) if len(sys.argv) > 2 else None
PORT, KEY = 18765, "live-sandbox-key-123"
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-API-Key": KEY}
results: list[dict] = []
console_errors: list[str] = []


def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""), flush=True)
    return ok


def api(method, path, **kw):
    r = requests.request(method, BASE + path, headers=H, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def wait_for(fn, timeout=600, every=2.0, label=""):
    t = time.time()
    while time.time() - t < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        time.sleep(every)
    raise TimeoutError(label or "condition")


def videos(status=None):
    return api("GET", "/api/videos" + (f"?status={status}" if status else ""))


# --------------------------------------------------------------------------- setup
shutil.rmtree(WORK, ignore_errors=True)
(WORK / "config").mkdir(parents=True)
(WORK / "screens").mkdir()
media = make_media(ROOT / "tests" / ".sandbox" / "media")
web = FakeWeb(media)
tiny = ROOT / "tests" / ".sandbox" / "tinysdxl"
if not (tiny / "model_index.json").exists():
    subprocess.run([sys.executable, str(ROOT / "tests" / "tools" / "make_tiny_sdxl.py"), str(tiny)], check=True, capture_output=True)
music = WORK / "music"
music.mkdir()
shutil.copy(media["music.mp3"], music / "track.mp3")
broll = WORK / "broll"
(broll / "city").mkdir(parents=True)
shutil.copy(media["clip_city.mp4"], broll / "city" / "traffic-night-rain.mp4")
shutil.copy(media["clip_server.mp4"], broll / "server-room-lights.mp4")

s = json.loads(json.dumps(SHIPPED_SETTINGS))
s["app"].update(data_dir=str(WORK / "data"), port=PORT)
s["llm"]["base_url"] = "http://ollama.local:11434"
s["video"]["music_dir"] = str(music)
s["media"]["broll_dir"] = str(broll)
s["ai_images"].update(model=str(tiny), vae=None, lightning_repo=None, lightning_file=None, steps=2,
                      short_size=[64, 112], long_size=[112, 64], cache_dir=str(WORK / "hf"))
tiny_ltx = ROOT / "tests" / ".sandbox" / "tinyltx"
if not (tiny_ltx / "model_index.json").exists():
    subprocess.run([sys.executable, str(ROOT / "tests" / "tools" / "make_tiny_ltx.py"), str(tiny_ltx)], check=True, capture_output=True)
s["ai_video"].update(model=str(tiny_ltx), state_file=str(WORK / "ai_video.json"), base_steps=2, short_size=[64, 96],
                     long_size=[96, 64], frames=9, max_clips={"short": 3, "long": 3})
s["app"]["timezone"] = "Asia/Dubai"   # non-UTC on purpose (slot pre-fill checks)
(WORK / "config" / "settings.yaml").write_text(yaml.safe_dump(s, sort_keys=False))
(WORK / "config" / "channels.yaml").write_text(yaml.safe_dump(CHANNELS, sort_keys=False))

env = {**os.environ, "YTM_CONFIG_DIR": str(WORK / "config"), "YTM_API_KEY": KEY, "YOUTUBE_API_KEY": "yt-test-key",
       "YTM_FAKEWEB_PORT": str(web.port), "YTM_ROOT_FOR_TESTS": str(ROOT),
       "PYTHONPATH": str(ROOT / "tests" / "live"), "PYTHONWARNINGS": "ignore"}
for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "DISPLAY"):
    env.pop(k, None)
server_log = open(WORK / "server.log", "w")
server = subprocess.Popen([sys.executable, "-m", "app.server"], cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
try:
    wait_for(lambda: requests.get(BASE + "/api/health", headers=H, timeout=5).status_code == 200, 90, 1, "server start")
    h = api("GET", "/api/health")
    check("server: starts as a separate process and answers /api/health", h["worker_alive"] and h["ollama"]["model_present"], h)
    check("server: rejects requests without the API key", requests.get(BASE + "/api/stats", timeout=5).status_code == 401)

    # ----------------------------------------------------------------------- dashboard (real browser)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        dialog_answers = {"prompt": KEY}

        dialogs = []

        def on_dialog(d):
            dialogs.append((d.type, d.message))
            if d.type == "prompt":
                d.accept(dialog_answers["prompt"])
            else:
                d.accept()
        page.on("dialog", on_dialog)
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        shot = lambda n: page.screenshot(path=str(WORK / "screens" / f"{n}.png"), full_page=True)
        tab = lambda t: (page.click(f"#tabs button[data-tab={t}]"), page.wait_for_timeout(800))

        def modal_ok(pg=None, text=None, area=None):
            pg = pg or page
            pg.wait_for_selector("#modal:not(.hidden)", timeout=10000)
            if text is not None:
                pg.fill("#modal-input", text)
            if area is not None:
                pg.fill("#modal-area", area)
            pg.click("#modal-ok")
            pg.wait_for_selector("#modal.hidden", state="attached", timeout=10000)

        page.goto(BASE + "/")
        modal_ok(text=KEY)   # first visit: in-app API key prompt
        page.wait_for_selector("#health .chip", timeout=20000)
        chips = page.inner_text("#health")
        check("dashboard: asks for the API key and shows the health bar", "Worker" in chips and "LLM qwen2.5" in chips, chips)
        shot("01-review-empty")

        tab("channels")
        page.wait_for_selector("#channel-list .card")
        check("dashboard: Channels tab lists both channels", page.locator("#channel-list .card").count() == 2)
        page.locator("#channel-list .card", has_text="Demo Channel").get_by_role("button", name="Run research now").click()
        shot("02-channels")
        wait_for(lambda: len(videos("idea_pending")) >= 3, 180, 2, "ideas")
        check("dashboard: 'Run research now' produces ideas", True)

        tab("review")
        page.wait_for_selector("#review-list .card")
        n_ideas = page.locator("#review-list .card").count()
        shot("03-ideas")
        check("dashboard: Review tab shows idea cards", n_ideas >= 3, n_ideas)
        # reject one idea through the UI, approve the rest with the bulk button
        first = page.locator("#review-list .card").first
        first.get_by_role("button", name="Reject").click()
        modal_ok()
        page.wait_for_timeout(1500)
        check("dashboard: Reject on an idea card", len(videos("rejected")) == 1)
        page.wait_for_function("document.querySelectorAll('#review-list .card').length === %d" % (n_ideas - 1), timeout=20000)
        netlog = []
        before_ids = [(v_["id"], v_["status"]) for v_ in videos()]
        page.on("response", lambda r: netlog.append(f"{r.request.method} {r.url.split(str(PORT))[1]} -> {r.status}") if "/api/videos" in r.url else None)
        page.click("#btn-approve-ideas")
        modal_ok()
        page.wait_for_timeout(400)
        try:
            wait_for(lambda: not videos("idea_pending"), 30, 1, "bulk approve")
            check("dashboard: 'Approve all visible ideas'", True)
        except TimeoutError:
            check("dashboard: 'Approve all visible ideas'", False, {"net": netlog, "dialogs": dialogs[-3:],
                  "select": page.eval_on_selector("#review-channel", "e => e.value"), "toast": page.inner_text("#toast"),
                  "ids_before": before_ids,
                  "resp": requests.get(BASE + "/api/videos?status=idea_pending", headers=H).json().__len__()})
            for v_ in videos("idea_pending"):
                api("POST", f"/api/videos/{v_['id']}/approve", json={})

        wait_for(lambda: len(videos("script_review")) >= 2, 240, 2, "scripts")
        page.click("#review-filter button[data-f=script_review]")
        page.wait_for_selector("#review-list .card >> text=Review & edit")
        shot("04-scripts")
        # 1) edit + save & approve through the drawer
        page.locator("#review-list .card").first.get_by_role("button", name="Review & edit").click()
        page.wait_for_selector("#drawer:not(.hidden) .s-text")
        vid_edit = int(re.search(r"#(\d+)", page.inner_text("#drawer-body .meta")).group(1))
        page.locator("#drawer-body .s-text").first.fill("Edited in the sandbox: your password is already leaked.")
        shot("05-script-drawer")
        page.get_by_role("button", name="Save & approve script").click()
        page.wait_for_timeout(1500)
        v = api("GET", f"/api/videos/{vid_edit}")
        check("dashboard: edit script in drawer, Save & approve", v["status"] in ("script_approved", "producing", "final_review")
              and v["script_text"].startswith("Edited in the sandbox"), v["status"])
        # 2) regenerate with notes
        page.click("#review-filter button[data-f=script_review]")
        page.wait_for_selector("#review-list .card >> text=Review & edit")
        page.locator("#review-list .card").first.get_by_role("button", name="Review & edit").click()
        page.wait_for_selector("#drawer:not(.hidden) .s-text")
        vid_redo = int(re.search(r"#(\d+)", page.inner_text("#drawer-body .meta")).group(1))
        page.get_by_role("button", name="Regenerate with notes…").click()
        modal_ok(area="make the hook punchier")
        page.wait_for_timeout(1500)
        v = api("GET", f"/api/videos/{vid_redo}")
        check("dashboard: 'Regenerate with notes' sends the script back with feedback",
              v["status"] in ("idea_approved", "scripting", "script_review") and v["feedback"] == "make the hook punchier", v)
        wait_for(lambda: api("GET", f"/api/videos/{vid_redo}")["status"] == "script_review", 600, 3, "regenerated")
        # 3) approve the rest straight from the cards
        page.click("#review-filter button[data-f=script_review]")
        page.wait_for_timeout(800)
        for _ in range(10):
            btn = page.locator("#review-list .card").get_by_role("button", name="Approve", exact=True)
            if not btn.count():
                break
            btn.first.click()
            page.wait_for_timeout(900)
        check("dashboard: Approve on script cards", not videos("script_review"))

        # production runs in the background worker
        t0 = time.time()
        wait_for(lambda: not videos("script_approved,producing") and videos("final_review"), 1500, 5, "production")
        finals = videos("final_review")
        failed = videos("failed")
        check(f"worker: produced {len(finals)} videos in {time.time() - t0:.0f}s with no failures", finals and not failed,
              [f["error"] for f in failed])
        prod = [json.loads(j["result_json"]) for j in api("GET", "/api/jobs?limit=500") if j["kind"] == "produce" and j["status"] == "done" and j["result_json"]]
        check("worker: AI animation step ran and its clips were used in the final videos",
              prod and all(p["animate"].get("clips", 0) >= 1 and p["video"]["animated"] == p["animate"]["clips"] for p in prod),
              [(p["animate"], p["video"].get("animated")) for p in prod])
        check("worker: animated 'pop' captions burned into Shorts and long videos",
              all(p["captions"]["style"] == "pop" and p["captions"]["burned_in"] for p in prod), [p["captions"] for p in prod])
        page.click("#review-filter button[data-f=final_review]")
        page.wait_for_selector("#review-list .card img.thumb")
        shot("06-finals")
        page.locator("#review-list .card").first.get_by_role("button", name="Watch & approve").click()
        page.wait_for_selector("#drawer:not(.hidden) video")
        vid_final = int(re.search(r"#(\d+)", page.inner_text("#drawer-body .meta")).group(1))
        src = page.get_attribute("#drawer-body video", "src")
        r = requests.get(BASE + src, timeout=30)
        check("dashboard: final video and thumbnail are served to the player", r.status_code == 200 and r.content[4:8] == b"ftyp")
        hist = page.inner_text("#drawer-body")
        check("dashboard: video history shows animation progress", "animate" in hist and "clip 1/" in hist)
        page.fill("#f-title", "Sandbox title edited in the dashboard")
        shot("07-final-drawer")
        page.get_by_role("button", name="Approve & schedule").click()
        page.wait_for_timeout(1500)
        v = api("GET", f"/api/videos/{vid_final}")
        check("dashboard: edit title + 'Approve & schedule'", v["status"] in ("approved", "uploading", "scheduled", "published")
              and v["title"] == "Sandbox title edited in the dashboard" and v["scheduled_at"], v["status"])
        for f in videos("final_review"):
            api("POST", f"/api/videos/{f['id']}/approve", json={})

        tab("pipeline")
        page.click("#btn-tick")
        wait_for(lambda: not videos("approved,uploading"), 120, 2, "uploads")
        up = videos("scheduled,published")
        check("upload: approved videos go to YouTube (fake) as scheduled", len(up) == len(finals), [(x["id"], x["status"]) for x in up])
        page.wait_for_timeout(1000)
        shot("08-pipeline")
        page.select_option("#pipe-status", "scheduled")
        page.wait_for_timeout(1000)
        check("dashboard: Pipeline status filter", page.locator("#pipe-body tr").count() == len(up))

        tab("analytics")
        page.click("#btn-analytics")
        wait_for(lambda: api("GET", "/api/analytics/summary")["videos"], 60, 2, "analytics")
        tab("jobs")
        tab("analytics")
        page.wait_for_selector("#analytics-body tr td.num")
        shot("09-analytics")
        check("dashboard: Analytics tab shows synced stats", page.locator("#analytics-body tr").count() == len(up))
        tab("jobs")
        page.wait_for_selector("#jobs-body tr")
        shot("10-jobs")
        jobs = api("GET", "/api/jobs?limit=500")
        check("jobs: every job finished, none failed", all(j["status"] in ("done", "cancelled") for j in jobs),
              [(j["kind"], j["status"], j["error"]) for j in jobs if j["status"] not in ("done", "cancelled")])

        # desktop screenshots in both colour schemes
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(color_scheme="dark")
        tab("review"); page.click("#review-filter button[data-f=all]"); page.wait_for_timeout(600); shot("d-review-dark")
        page.emulate_media(color_scheme="light"); page.wait_for_timeout(300); shot("d-review-light")
        tab("pipeline"); page.select_option("#pipe-status", ""); page.wait_for_timeout(800); shot("d-pipeline-light")
        page.emulate_media(color_scheme="dark")
        page.fill("#pipe-search", "zzz-no-such-title"); page.wait_for_timeout(300)
        check("dashboard: pipeline search filters rows", "No titles match" in page.inner_text("#pipe-body"))
        page.fill("#pipe-search", ""); page.wait_for_timeout(300)
        check("dashboard: no JavaScript errors in the browser console (desktop)", not console_errors, console_errors[:5])

        # ------------------------------------------------------------------- phone (touch, 390x844) + tablet
        api("POST", "/api/research/run", json={"channel": "tech-simplified"})   # fresh ideas to act on from the phone
        wait_for(lambda: len(videos("idea_pending")) >= 3, 180, 2, "phone ideas")
        mctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
        m = mctx.new_page()
        m.emulate_media(color_scheme="dark")
        m.on("pageerror", lambda e: console_errors.append("phone: " + str(e)))
        m.on("console", lambda msg: console_errors.append("phone: " + msg.text) if msg.type == "error" else None)
        mshot = lambda n: m.screenshot(path=str(WORK / "screens" / f"{n}.png"))
        mtab = lambda t: (m.tap(f"#tabs button[data-tab={t}]"), m.wait_for_timeout(900))
        m.goto(BASE + "/")
        m.wait_for_selector("#modal:not(.hidden)")
        m.wait_for_timeout(500)   # let the slide-up animation finish
        mb = m.locator("#modal .modal-box").bounding_box()
        mshot("p-00-key-sheet")
        check("phone: API key prompt is a bottom sheet", abs(mb["y"] + mb["height"] - 844) < 2 and mb["width"] >= 388, mb)
        modal_ok(m, text=KEY)
        m.wait_for_selector("#review-list .card")
        nav = m.locator("#tabs").bounding_box()
        check("phone: tabs become a bottom navigation bar", abs(nav["y"] + nav["height"] - 844) < 2 and nav["width"] >= 388, nav)
        heights = m.eval_on_selector_all("#tabs button, #review-list .card .actions button", "els => els.map(e => e.getBoundingClientRect().height)")
        check("phone: buttons are thumb-sized (>= 44 px tall)", heights and min(heights) >= 44, heights[:8])
        fsz = m.eval_on_selector_all("input, select, textarea", "els => els.filter(e => e.offsetParent).map(e => parseFloat(getComputedStyle(e).fontSize))")
        check("phone: form fields use 16 px text (no iOS zoom-in)", all(f >= 16 for f in fsz), fsz)
        mshot("p-01-review")
        n_before = len(videos("idea_pending"))
        m.locator("#review-list .card").first.get_by_role("button", name="Approve", exact=True).tap()
        wait_for(lambda: len(videos("idea_pending")) == n_before - 1, 20, 1, "phone approve")
        check("phone: Approve an idea with a tap", True)
        m.wait_for_timeout(800)
        m.locator("#review-list .card").first.get_by_role("button", name="Reject").tap()
        m.wait_for_selector("#modal:not(.hidden)")
        m.wait_for_timeout(500)
        mshot("p-02-reject-sheet")
        modal_ok(m)
        wait_for(lambda: len(videos("idea_pending")) == n_before - 2, 20, 1, "phone reject")
        check("phone: Reject with the confirm sheet", True)

        overflow = {}
        for t in ("review", "pipeline", "channels", "analytics", "jobs"):
            mtab(t)
            overflow[t] = m.evaluate("document.scrollingElement.scrollWidth")
            mshot(f"p-tab-{t}")
            m.screenshot(path=str(WORK / "screens" / f"p-tab-{t}-full.png"), full_page=True)
        check("phone: no sideways scrolling on any tab (390 px)", all(w <= 391 for w in overflow.values()), overflow)
        mtab("pipeline")
        disp = m.evaluate("[getComputedStyle(document.querySelector('.grid.stack thead')).display, getComputedStyle(document.querySelector('#pipe-body tr')).display]")
        check("phone: pipeline table turns into cards", disp == ["none", "grid"], disp)
        m.locator("#pipe-body tr.click", has_text="Sandbox title edited").first.tap()
        m.wait_for_selector("#drawer:not(.hidden) video")
        m.wait_for_timeout(500)
        pw_ = m.locator(".drawer-panel").bounding_box(); vw = m.locator("#drawer-body video").bounding_box()
        check("phone: video details open full-screen, player fits", pw_["width"] >= 389 and vw["width"] <= 390 and vw["x"] >= 0, (pw_, vw))
        check("phone: Download video button in details", m.locator("#drawer-body a.dl", has_text="Download video").count() == 1)
        r = requests.get(BASE + m.get_attribute("#drawer-body a.dl", "href"), timeout=30)
        check("phone: download link serves the mp4", r.status_code == 200 and r.content[4:8] == b"ftyp")
        mshot("p-03-video-drawer")
        m.tap("#drawer-close"); m.wait_for_timeout(400)
        check("phone: back button closes details", m.locator("#drawer.hidden").count() == 1)
        mtab("review")
        m.locator("#review-list .card").first.get_by_role("button", name="Approve", exact=True).wait_for()
        m.evaluate("openVideo(%d)" % videos("idea_pending")[0]["id"])
        m.wait_for_selector("#drawer:not(.hidden) .sticky-actions button")
        m.wait_for_timeout(500)
        sa = m.locator(".sticky-actions").bounding_box()
        check("phone: action bar stays pinned at the bottom of the details page", sa and sa["y"] + sa["height"] <= 845 and sa["y"] > 600, sa)
        mshot("p-04-idea-drawer")
        m.tap("#drawer-close")
        m.emulate_media(color_scheme="light"); mtab("review"); mshot("p-05-review-light"); m.emulate_media(color_scheme="dark")
        m.set_viewport_size({"width": 820, "height": 1180}); mtab("review")
        check("tablet: no sideways scrolling (820 px)", m.evaluate("document.scrollingElement.scrollWidth") <= 821)
        mshot("t-01-review")
        mctx.close()
        check("dashboard: no JavaScript errors in the browser console", not console_errors, console_errors[:5])
        browser.close()

    # ----------------------------------------------------------------------- n8n workflows
    if N8N_BIN:
        n8n_home = WORK / "n8n"
        wf_dir = WORK / "n8n-import"
        wf_dir.mkdir()
        hook = web.url("hooks.local", "/alert")
        for f in sorted((ROOT / "n8n").glob("*.json")):
            txt = f.read_text().replace("PASTE_YTM_API_KEY", KEY).replace("http://127.0.0.1:8765", BASE)
            d = json.loads(txt)
            for node in d["nodes"]:
                if node["name"] == "Config":
                    cfgj = json.loads(node["parameters"]["jsonOutput"])
                    cfgj["notifyWebhook"] = hook
                    node["parameters"]["jsonOutput"] = json.dumps(cfgj)
                if node["type"] == "n8n-nodes-base.wait":
                    node["parameters"].update(amount=3, unit="seconds")  # 2 min in production; shortened for the sandbox
            (wf_dir / f.name).write_text(json.dumps(d))
        nenv = {**os.environ, "N8N_USER_FOLDER": str(n8n_home), "N8N_DIAGNOSTICS_ENABLED": "false", "GENERIC_TIMEZONE": "Asia/Dubai",
                "N8N_RUNNERS_ENABLED": "true", "PATH": f"{os.environ.get('NODE24_BIN', '')}:{N8N_BIN}:{os.environ['PATH']}", "N8N_LOG_LEVEL": "warn",
                "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false", "N8N_LISTEN_ADDRESS": "127.0.0.1"}
        n8n = ["node", str(N8N_BIN.parent.parent / "n8n" / "bin" / "n8n")] if (N8N_BIN.parent.parent / "n8n").exists() else ["n8n"]
        def n8n_cmd(*args, timeout=300):
            r = subprocess.run(n8n + list(args), env=nenv, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout + r.stderr

        # manual-trigger copies: run every node of each workflow right now (the CLI cannot start from a schedule)
        man_dir = WORK / "n8n-manual"
        man_dir.mkdir()
        manual_ids = {}
        for f in sorted(wf_dir.glob("*.json")):
            d = json.loads(f.read_text())
            mid = f"YtmManual{f.name[:2]}000000"[:16]
            manual_ids[f.name[:2]] = mid
            d["id"], d["name"] = mid, d["name"] + " (manual test copy)"
            for node in d["nodes"]:
                if node["type"] == "n8n-nodes-base.scheduleTrigger":
                    node.update(type="n8n-nodes-base.manualTrigger", typeVersion=1, parameters={})
            (man_dir / f.name).write_text(json.dumps(d))
        code, out = n8n_cmd("import:workflow", "--separate", f"--input={wf_dir}")
        code2, out2 = n8n_cmd("import:workflow", "--separate", f"--input={man_dir}")
        code3, listing = subprocess.run(n8n + ["list:workflow"], env={**nenv, "N8N_LOG_LEVEL": "info"}, capture_output=True, text=True, timeout=300).returncode, ""
        listing = subprocess.run(n8n + ["list:workflow"], env={**nenv, "N8N_LOG_LEVEL": "info"}, capture_output=True, text=True, timeout=300).stdout
        shipped = [i for i in ("YtmResearch00001", "YtmPipeTick00002", "YtmReviewAlrt003", "YtmAnalytics0004", "YtmHealthWtch005") if f"{i}|" in listing]
        check("n8n: imports all 5 shipped workflows (n8n 2.x)", len(shipped) == 5, {"exit": code, "listed": shipped})

        def run_wf(num):
            code_, out_ = n8n_cmd("execute", f"--id={manual_ids[num]}")
            with open(WORK / f"n8n-exec-{num}.log", "a") as fh:
                fh.write(f"=== exit {code_}\n{out_}\n")
            return code_, out_

        before = len(api("GET", "/api/jobs?limit=500"))
        code, out = run_wf("01")
        jobs = api("GET", "/api/jobs?limit=500")
        check("n8n 01 Daily research: queues research for every channel", code == 0 and len(jobs) - before == 2, out[-400:])
        wait_for(lambda: all(j["status"] not in ("queued", "running") for j in api("GET", "/api/jobs?limit=500")), 400, 2, "research")

        code, out = run_wf("02")
        check("n8n 02 Pipeline tick: calls /api/pipeline/tick", code == 0 and "NOT successful" not in out and "ECONNREFUSED" not in out,
              [l for l in out.splitlines() if "deferred" in l or "successful" in l.lower()][:5])

        web.hooks.clear()
        code, out = run_wf("03")
        alerts = [h for h in web.hooks if "need review" in json.dumps(h)]
        check("n8n 03 Review alerts: posts 'items need review' to the webhook", code == 0 and alerts, (out[-300:], web.hooks))
        web.hooks.clear()
        code, out = run_wf("03")
        check("n8n 03 Review alerts: does not repeat the same alert", code == 0 and not web.hooks, web.hooks)

        web.hooks.clear()
        code, out = run_wf("04")
        digest = [h for h in web.hooks if "daily digest" in json.dumps(h)]
        check("n8n 04 Analytics + digest: syncs stats, waits, posts the digest", code == 0 and digest, (out[-300:], web.hooks))
        if digest:
            check("n8n 04: webhook body has Discord 'content' and Slack 'text'", {"content", "text"} <= set(digest[0]["body"]), digest[0]["body"])

        web.hooks.clear()
        code, out = run_wf("05")
        check("n8n 05 Health watchdog: silent while healthy", code == 0 and not web.hooks, (out[-300:], web.hooks))

        # the real scheduled workflows: publish all 5 and run n8n as a server; a sped-up copy of 03
        # (cron every minute) proves the Schedule trigger fires and reaches the API by itself
        fast = json.loads((wf_dir / "03-review-alerts.json").read_text())
        fast["id"], fast["name"] = "YtmFastAlerts006", "fast copy of 03 (every minute)"
        for node in fast["nodes"]:
            if node["type"] == "n8n-nodes-base.scheduleTrigger":
                node["parameters"]["rule"]["interval"][0]["expression"] = "* * * * *"
        fdir = WORK / "n8n-fast"
        fdir.mkdir()
        (fdir / "fast.json").write_text(json.dumps(fast))
        n8n_cmd("import:workflow", "--separate", f"--input={fdir}")
        pub = [n8n_cmd("publish:workflow", f"--id={i}")[0] for i in
               ("YtmResearch00001", "YtmPipeTick00002", "YtmReviewAlrt003", "YtmAnalytics0004", "YtmHealthWtch005", "YtmFastAlerts006")]
        check("n8n: publishes (activates) the 5 workflows from the command line", all(c == 0 for c in pub), pub)
        api("POST", "/api/research/run", json={"channel": "tech-simplified"})
        wait_for(lambda: all(j["status"] not in ("queued", "running") for j in api("GET", "/api/jobs?limit=500")), 400, 2, "research 2")
        web.hooks.clear()
        n8n_log = open(WORK / "n8n-server.log", "w")
        n8n_srv = subprocess.Popen(n8n + ["start"], env={**nenv, "N8N_PORT": "5699", "N8N_LOG_LEVEL": "info"},
                                   stdout=n8n_log, stderr=subprocess.STDOUT)
        try:
            up = wait_for(lambda: requests.get("http://127.0.0.1:5699/healthz", timeout=3).status_code == 200, 180, 2, "n8n up")
            check("n8n: server starts with the published workflows", up)
            fired = wait_for(lambda: [h for h in web.hooks if "need review" in json.dumps(h)], 150, 3, "schedule fired")
            check("n8n: a Schedule trigger fires on its own and reaches the API + webhook", fired)
        except TimeoutError as e:
            check("n8n: server mode schedule test", False, e)
        finally:
            n8n_srv.terminate()
            try:
                n8n_srv.wait(timeout=30)
            except subprocess.TimeoutExpired:
                n8n_srv.kill()
        log_txt = (WORK / "n8n-server.log").read_text()
        check("n8n: no activation errors in the server log", "problem activating" not in log_txt.lower() and "could not be activated" not in log_txt.lower(),
              [l for l in log_txt.splitlines() if "activat" in l.lower()][:5])

        server.send_signal(signal.SIGINT)
        server.wait(timeout=30)
        web.hooks.clear()
        code, out = run_wf("05")
        check("n8n 05 Health watchdog: alerts when the API is down", web.hooks and "health check failed" in json.dumps(web.hooks), (code, out[-300:]))
finally:
    if server.poll() is None:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
    web.stop()
    (WORK / "results.json").write_text(json.dumps({"checks": results, "console_errors": console_errors}, indent=1))
    print(f"\n{sum(r['ok'] for r in results)}/{len(results)} live checks passed")
