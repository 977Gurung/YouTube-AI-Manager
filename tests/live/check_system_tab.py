"""Browser check of the dashboard's System tab (27 checks, desktop + phone).

Needs a running server with a simulated GPU, e.g.:
    mkdir -p /tmp/fakebin && cp tests/live/fake-nvidia-smi /tmp/fakebin/nvidia-smi
    PATH=/tmp/fakebin:$PATH YTM_API_KEY=k YTM_CONFIG_DIR=<config dir with app.port: 8791> python -m app.server
then:
    python tests/live/check_system_tab.py <screenshot dir>
Requires Playwright with Chromium. The API key "k" and port 8791 are test values.
"""
import sys, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8791"; OUT=sys.argv[1]; res=[]; errors=[]
def check(name, ok, detail=""):
    res.append(ok); print(("PASS " if ok else "FAIL ")+name+("" if ok else f" - {detail}"), flush=True)
INK = "(id) => { const c=document.getElementById(id)||document.querySelector(id); const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data; let n=0; for(let i=3;i<d.length;i+=4) if(d[i]>40) n++; return n; }"
SIG = "(id) => { const c=document.getElementById(id); const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data; let h=0; for(let i=0;i<d.length;i+=97) h=(h*31+d[i])>>>0; return h; }"
with sync_playwright() as pw:
    b=pw.chromium.launch()
    ctx=b.new_context(viewport={"width":1440,"height":1000}, color_scheme="dark"); ctx.add_init_script("localStorage.setItem('ytm_key','k')")
    p=ctx.new_page(); p.on("pageerror", lambda e: errors.append(str(e))); p.on("console", lambda m: errors.append(m.text) if m.type=="error" else None)
    p.goto(BASE+"/"); p.wait_for_selector("#review-list .empty, #review-list .card")
    check("app still boots on the Review tab; 6 tabs incl. System", p.locator("#tabs button").count()==6 and p.locator("#tab-review.active").count()==1)
    check("no polling of /api/system while another tab is open", p.evaluate("sysmon.timer===null && sysmon.samples.length===0"))
    p.click("#tabs button[data-tab=system]"); p.wait_for_selector("#perf-rail .rail-item"); p.wait_for_timeout(7000)
    check("rail lists CPU, Memory, Disk, Network, GPU with live values", p.locator(".rail-item b").all_inner_texts()==["CPU","Memory","Disk","Network","GPU"] and "%" in p.inner_text("#rail-sub-cpu") and "GB" in p.inner_text("#rail-sub-mem"), p.locator(".rail-sub").all_inner_texts())
    check("six ring gauges with values", p.locator("#sys-gauges .gauge").count()==6 and all("%" in t or "°" in t for t in p.locator(".gauge-val b").all_inner_texts()), p.locator(".gauge-val b").all_inner_texts())
    n0=p.evaluate("sysmon.samples.length"); p.wait_for_timeout(5000); n1=p.evaluate("sysmon.samples.length")
    check("new samples arrive every ~2 s", 2 <= n1-n0 <= 4, (n0,n1))
    check("CPU graph is drawn", p.evaluate(INK,"c-cpu") > 2000)
    a=p.evaluate(SIG,"c-cpu"); p.wait_for_timeout(400); c=p.evaluate(SIG,"c-cpu")
    check("graph scrolls smoothly between samples (changes within 0.4 s)", a!=c)
    check("every rail thumbnail is drawn", all(p.evaluate(INK, f".rail-item[data-id={i}] canvas") > 100 for i in ["cpu","mem","disk","net","gpu"]))
    p.click("#cpu-mode button[data-m=cores]"); p.wait_for_timeout(600)
    cores=p.evaluate("sysmon.info.cpu_threads")
    check("logical processors view: one graph per core", p.locator(".core canvas").count()==cores and p.evaluate(INK,"c-core-0")>50, cores)
    for sel, cid, stat in [("mem","c-mem","s-used"),("disk","c-diskio","s-read"),("net","c-net","s-rx"),("gpu","c-vram","s-vram")]:
        p.click(f".rail-item[data-id={sel}]"); p.wait_for_timeout(2500)
        check(f"{sel} pane: graph drawn and stats filled", p.evaluate(INK,cid)>300 and p.inner_text("#"+stat) not in ("","—"), p.inner_text("#"+stat))
    check("GPU pane shows 4 graphs, name and power", p.locator("#perf-pane canvas").count()==4 and "RTX 2070" in p.inner_text("#pane-sub") and "W" in p.inner_text("#s-gpow"))
    check("GPU process list", "python" in p.inner_text("#sys-gpu-procs") and "ollama" in p.inner_text("#sys-gpu-procs"))
    check("process list shows names, CPU and memory", p.locator("#sys-procs tr").count()>=3 and "%" in p.inner_text("#sys-procs"))
    p.click(".rail-item[data-id=net]"); p.wait_for_timeout(500)
    nic=p.locator("#nic option").all_inner_texts(); 
    if len(nic)>1:
        p.select_option("#nic", nic[1]); p.wait_for_timeout(2500)
    check("network adapter selector", len(nic)>=2 and p.inner_text("#s-nic")==nic[1] and p.evaluate(INK,"c-net")>300, nic)
    p.click("#sys-window button[data-w='300']"); p.wait_for_timeout(600)
    check("time window switch (5 min)", p.evaluate("sysmon.win")==300 and "5 minutes" in p.inner_text(".chart-foot"))
    p.emulate_media(color_scheme="light"); p.wait_for_timeout(600); p.screenshot(path=OUT+"/c-light.png")
    check("light theme: graph still drawn", p.evaluate(INK,"c-net")>300)
    p.click("#tabs button[data-tab=jobs]"); p.wait_for_timeout(2600)
    n2=p.evaluate("sysmon.samples.length"); p.wait_for_timeout(4500)
    check("polling and animation stop when you leave the tab", p.evaluate("sysmon.samples.length")==n2 and p.evaluate("sysmon.timer===null && sysmon.raf===0"), n2)
    p.click("#tabs button[data-tab=system]"); p.wait_for_timeout(3000)
    check("coming back resumes, keeps history", p.evaluate("sysmon.samples.length")>n2)
    p.goto(BASE+"/#system"); p.wait_for_selector("#perf-rail .rail-item"); p.wait_for_timeout(2500)
    check("opening directly on #system loads the server's history at once", p.evaluate("sysmon.samples.length")>10, p.evaluate("sysmon.samples.length"))
    # phone
    m=b.new_context(viewport={"width":390,"height":844}, is_mobile=True, has_touch=True, device_scale_factor=2, color_scheme="dark"); m.add_init_script("localStorage.setItem('ytm_key','k')")
    q=m.new_page(); q.on("pageerror", lambda e: errors.append("phone "+str(e))); q.on("console", lambda mm: errors.append("phone "+mm.text) if mm.type=="error" else None)
    q.goto(BASE+"/"); q.wait_for_selector("#review-list .empty, #review-list .card")
    w=q.eval_on_selector_all("#tabs button","els=>els.map(e=>[e.getBoundingClientRect().width, e.scrollWidth<=e.clientWidth+1, e.getBoundingClientRect().height])")
    check("phone: 6 bottom-nav buttons fit, thumb-sized", len(w)==6 and all(x[0]>=60 and x[1] and x[2]>=44 for x in w) and q.evaluate("document.scrollingElement.scrollWidth")<=391, w)
    q.tap("#tabs button[data-tab=system]"); q.wait_for_selector("#perf-rail .rail-item"); q.wait_for_timeout(5000)
    check("phone: no sideways page scroll on System", q.evaluate("document.scrollingElement.scrollWidth")<=391)
    check("phone: resource strip scrolls sideways inside itself", q.evaluate("(()=>{const r=document.getElementById('perf-rail'); return r.scrollWidth>r.clientWidth && getComputedStyle(r).flexDirection==='row'})()"))
    q.locator(".rail-item[data-id=gpu]").scroll_into_view_if_needed(); q.tap(".rail-item[data-id=gpu]"); q.wait_for_timeout(2500)
    bb=q.locator("#c-gpu").bounding_box()
    check("phone: GPU graphs fit the screen and are drawn", bb["x"]>=0 and bb["x"]+bb["width"]<=390 and q.evaluate(INK,"c-gpu")>300, bb)
    q.screenshot(path=OUT+"/c-phone.png")
    check("no JavaScript errors", not errors, errors[:4])
    b.close()
print(f"{sum(res)}/{len(res)} checks passed")
