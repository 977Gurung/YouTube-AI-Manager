// System tab: Task Manager-style live performance view (CPU, memory, disk, network, GPU).
// Plain canvas, no libraries. Polls /api/system every couple of seconds while the tab is open and
// scrolls the graphs smoothly between samples.
(() => {
  const { $, $$, esc, api, state, loaders, toast, setHTML } = window.YTM;
  const COLORS = { cpu: "#3b9eff", mem: "#a56eff", disk: "#34c98a", net: "#ffa53b", gpu: "#22cfc0", alt: "#ff6b9a" };
  const sys = { samples: [], last: 0, sel: "cpu", win: 60, interval: 2, history: 600, offset: 0, info: {}, view: {}, timer: null, raf: 0, cpuMode: "overall", nic: "" };
  window.sysmon = sys;

  // ---------------------------------------------------------------- formatting
  const f1 = (v) => (v == null ? "—" : (Math.round(v * 10) / 10).toLocaleString());
  const pct = (v) => (v == null ? "—" : Math.round(v) + "%");
  const gb = (mb) => (mb == null ? "—" : (mb / 1024).toFixed(1) + " GB");
  function bits(bytesPerSec) {
    const b = (bytesPerSec || 0) * 8;
    return b >= 1e9 ? (b / 1e9).toFixed(2) + " Gbps" : b >= 1e6 ? (b / 1e6).toFixed(1) + " Mbps" : b >= 1e3 ? (b / 1e3).toFixed(0) + " Kbps" : Math.round(b) + " bps";
  }
  function rate(bytesPerSec) {
    const b = bytesPerSec || 0;
    return b >= 1e9 ? (b / 1e9).toFixed(2) + " GB/s" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB/s" : b >= 1e3 ? (b / 1e3).toFixed(0) + " KB/s" : Math.round(b) + " B/s";
  }
  function uptime(s) {
    if (s == null) return "—";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + "d " : "") + String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(Math.floor(s % 60)).padStart(2, "0");
  }
  const niceMax = (v, floor) => { v = Math.max(v, floor); const p = Math.pow(10, Math.floor(Math.log10(v))); const n = v / p; return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p; };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const last = () => sys.samples[sys.samples.length - 1];
  const nowS = () => Date.now() / 1000 + sys.offset - sys.interval;   // lag one interval so new points slide in

  // ---------------------------------------------------------------- data series
  const nicOf = (s) => (sys.nic && s.net.ifaces[sys.nic]) || s.net;
  const SERIES = {
    cpu: (s) => s.cpu.pct, mem: (s) => s.mem.used_mb, memPct: (s) => s.mem.pct,
    diskBusy: (s) => s.disk.busy, diskR: (s) => s.disk.read, diskW: (s) => s.disk.write,
    netRx: (s) => nicOf(s).rx, netTx: (s) => nicOf(s).tx,
    gpu: (s) => (s.gpu.ok ? s.gpu.util : null), vram: (s) => (s.gpu.ok ? s.gpu.mem_used_mb : null),
    vramPct: (s) => (s.gpu.ok ? s.gpu.mem_pct : null), gpuTemp: (s) => (s.gpu.ok ? s.gpu.temp_c : null), gpuPower: (s) => (s.gpu.ok ? s.gpu.power_w : null),
  };
  const pts = (fn) => sys.samples.map((s) => [s.t, fn(s)]);
  const windowMax = (fns, floor) => {
    const from = nowS() - sys.win; let m = 0;
    for (const s of sys.samples) if (s.t >= from - sys.interval) for (const fn of fns) m = Math.max(m, fn(s) || 0);
    return niceMax(m * 1.15, floor);
  };

  // ---------------------------------------------------------------- canvas chart
  function draw(cv, o) {
    const r = cv.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const dpr = window.devicePixelRatio || 1, W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    const g = cv.getContext("2d"), now = o.now, win = o.win, color = o.series[0].color;
    g.clearRect(0, 0, W, H);
    g.lineWidth = dpr; g.strokeStyle = o.thumb ? "transparent" : sys.view.grid;
    if (!o.thumb) {                                   // Task Manager grid: it scrolls with time
      g.beginPath();
      for (let i = 1; i < 4; i++) { const y = Math.round((H * i) / 4) + 0.5; g.moveTo(0, y); g.lineTo(W, y); }
      const step = win / 6, off = ((now % step) / win) * W;
      for (let x = W - off; x > 0; x -= (step / win) * W) { const xx = Math.round(x) + 0.5; g.moveTo(xx, 0); g.lineTo(xx, H); }
      g.stroke();
    }
    for (const s of o.series) {
      const X = (t) => W - ((now - t) / win) * W, Y = (v) => H - Math.max(0, Math.min(1, v / o.max)) * (H - 2 * dpr) - dpr;
      let seg = [], segs = [], prevT = null;
      for (const [t, v] of s.pts) {
        if (t < now - win - sys.interval * 2) continue;
        if (v == null || (prevT != null && t - prevT > sys.interval * 3.5)) { if (seg.length) segs.push(seg); seg = []; }
        if (v != null) seg.push([X(t), Y(v)]);
        prevT = t;
      }
      if (seg.length) segs.push(seg);
      for (const a of segs) {
        if (a.length < 2) continue;
        g.beginPath(); g.moveTo(a[0][0], a[0][1]);
        for (let i = 1; i < a.length; i++) g.lineTo(a[i][0], a[i][1]);
        g.lineWidth = (o.thumb ? 1.2 : 1.6) * dpr; g.strokeStyle = s.color; g.lineJoin = "round";
        if (s.dash) g.setLineDash([4 * dpr, 3 * dpr]);
        g.stroke(); g.setLineDash([]);
        if (s.fill !== false) {
          g.lineTo(a[a.length - 1][0], H); g.lineTo(a[0][0], H); g.closePath();
          g.globalAlpha = s.dash ? 0.06 : 0.16; g.fillStyle = s.color; g.fill(); g.globalAlpha = 1;
        }
      }
    }
    g.lineWidth = dpr; g.strokeStyle = color; g.globalAlpha = o.thumb ? 0.55 : 0.8;
    g.strokeRect(0.5 * dpr, 0.5 * dpr, W - dpr, H - dpr); g.globalAlpha = 1;
  }

  // ---------------------------------------------------------------- rail (left list with mini graphs)
  const ITEMS = [
    { id: "cpu", name: "CPU", color: COLORS.cpu, series: () => [{ pts: pts(SERIES.cpu), color: COLORS.cpu }], max: () => 100,
      sub: (s) => `${pct(s.cpu.pct)}${s.cpu.mhz ? "  " + (s.cpu.mhz / 1000).toFixed(2) + " GHz" : ""}` },
    { id: "mem", name: "Memory", color: COLORS.mem, series: () => [{ pts: pts(SERIES.memPct), color: COLORS.mem }], max: () => 100,
      sub: (s) => `${gb(s.mem.used_mb).replace(" GB", "")}/${gb(s.mem.total_mb)} (${pct(s.mem.pct)})` },
    { id: "disk", name: "Disk", color: COLORS.disk, series: () => [{ pts: pts(SERIES.diskBusy), color: COLORS.disk }], max: () => 100,
      sub: (s) => `${pct(s.disk.busy)} active` },
    { id: "net", name: "Network", color: COLORS.net,
      series: () => [{ pts: pts(SERIES.netRx), color: COLORS.net }, { pts: pts(SERIES.netTx), color: COLORS.net, dash: true }],
      max: () => windowMax([SERIES.netRx, SERIES.netTx], 12500), sub: (s) => `S: ${bits(nicOf(s).tx)}  R: ${bits(nicOf(s).rx)}` },
    { id: "gpu", name: "GPU", color: COLORS.gpu, series: () => [{ pts: pts(SERIES.gpu), color: COLORS.gpu }], max: () => 100,
      sub: (s) => (s.gpu.ok ? `${pct(s.gpu.util)}${s.gpu.temp_c != null ? " (" + Math.round(s.gpu.temp_c) + " °C)" : ""}` : "not detected") },
  ];
  function renderRail() {
    const rail = $("#perf-rail");
    if (!rail.children.length) {
      rail.innerHTML = ITEMS.map((it) => `<button class="rail-item" data-id="${it.id}" role="tab" style="--c:${it.color}">
        <canvas class="thumb-chart"></canvas><span class="rail-text"><b>${it.name}</b><span class="rail-sub" id="rail-sub-${it.id}">—</span></span></button>`).join("");
      $$(".rail-item", rail).forEach((b) => (b.onclick = () => select(b.dataset.id)));
    }
    const s = last();
    $$(".rail-item", rail).forEach((b) => b.classList.toggle("active", b.dataset.id === sys.sel));
    if (s) for (const it of ITEMS) $("#rail-sub-" + it.id).textContent = it.sub(s);
  }
  function select(id) { sys.sel = id; $("#perf-pane")._key = ""; render(); }

  // ---------------------------------------------------------------- gauges (ring diagram)
  function ring(label, value, text, color, hint = "") {
    const v = value == null ? 0 : Math.max(0, Math.min(100, value)), c = 2 * Math.PI * 42;
    const tone = value == null ? "var(--line2)" : v >= 92 ? "var(--bad)" : v >= 80 ? "var(--warn)" : color;
    return `<div class="gauge" title="${esc(hint)}"><svg viewBox="0 0 100 100" class="ring"><circle cx="50" cy="50" r="42" class="ring-bg"/>
      <circle cx="50" cy="50" r="42" class="ring-fg" style="stroke:${tone};stroke-dasharray:${(v / 100) * c} ${c}"/></svg>
      <div class="gauge-val"><b>${text}</b><span>${label}</span></div></div>`;
  }
  function renderGauges() {
    const s = last();
    if (!s) return;
    const g = s.gpu;
    setHTML($("#sys-gauges"), [
      ring("CPU", s.cpu.pct, pct(s.cpu.pct), COLORS.cpu, sys.info.cpu_model || ""),
      ring("Memory", s.mem.pct, pct(s.mem.pct), COLORS.mem, `${gb(s.mem.used_mb)} of ${gb(s.mem.total_mb)}`),
      ring("GPU", g.ok ? g.util : null, g.ok ? pct(g.util) : "n/a", COLORS.gpu, g.ok ? g.name : g.error || ""),
      ring("GPU memory", g.ok ? g.mem_pct : null, g.ok ? pct(g.mem_pct) : "n/a", COLORS.gpu, g.ok ? `${gb(g.mem_used_mb)} of ${gb(g.mem_total_mb)}` : ""),
      ring("GPU temp", g.ok && g.temp_c != null ? g.temp_c : null, g.ok && g.temp_c != null ? Math.round(g.temp_c) + "°" : "n/a", COLORS.net, "92 °C and above is shown in red"),
      ring("Disk used", s.disk.pct, pct(s.disk.pct), COLORS.disk, s.disk.free_gb != null ? `${s.disk.free_gb} GB free` : ""),
    ].join(""));
    const j = sys.job;
    setHTML($("#sys-job"), `<span class="dot ${j ? "on" : ""}"></span>${j ? `Worker is running <b>${esc(j.kind)}</b>${j.video_id ? ` for video #${j.video_id}` : ""}` : "Worker idle"}
      <span class="muted">· ${esc(sys.info.hostname || "")} · up ${uptime(sys.info.uptime_s)}</span>`);
  }

  // ---------------------------------------------------------------- right pane
  const stat = (label, value, id = "") => `<div class="stat"><span>${label}</span><b ${id ? `id="${id}"` : ""}>${value}</b></div>`;
  const chartBox = (id, title, right, cls = "") => `<div class="chart ${cls}"><div class="chart-head"><span>${title}</span><span id="${id}-max">${right}</span></div><canvas id="${id}"></canvas>
      <div class="chart-foot"><span id="${id}-win"></span><span>0</span></div></div>`;

  const PANES = {
    cpu: {
      title: "CPU", sub: () => sys.info.cpu_model || "",
      key: () => sys.cpuMode + ":" + (last() ? last().cpu.cores.length : 0),
      html: (s) => `<div class="pane-tools"><div class="seg" id="cpu-mode"><button data-m="overall" class="${sys.cpuMode === "overall" ? "active" : ""}">Overall</button>
          <button data-m="cores" class="${sys.cpuMode === "cores" ? "active" : ""}">Logical processors</button></div></div>` +
        (sys.cpuMode === "overall" ? chartBox("c-cpu", "% Utilization", "100%", "big")
          : `<div class="chart-head"><span>% Utilization per logical processor</span><span>100%</span></div><div class="core-grid" style="--n:${Math.ceil(Math.sqrt(s.cpu.cores.length * 1.6))}">` +
            s.cpu.cores.map((_, i) => `<div class="core"><canvas id="c-core-${i}"></canvas><span>CPU ${i}</span></div>`).join("") + `</div>`) +
        `<div class="stats">${stat("Utilization", "", "s-cpu")}${stat("Speed", "", "s-mhz")}${stat("Temperature", "", "s-ctemp")}${stat("Load (1 min)", "", "s-load")}
          ${stat("Cores", sys.info.cpu_cores || "—")}${stat("Logical processors", sys.info.cpu_threads || s.cpu.cores.length)}${stat("Up time", "", "s-up")}</div>`,
      after: () => $$("#cpu-mode button").forEach((b) => (b.onclick = () => { sys.cpuMode = b.dataset.m; $("#perf-pane")._key = ""; render(); })),
      charts: (s) => sys.cpuMode === "overall"
        ? [["c-cpu", [{ pts: pts(SERIES.cpu), color: COLORS.cpu }], 100]]
        : s.cpu.cores.map((_, i) => [`c-core-${i}`, [{ pts: sys.samples.map((x) => [x.t, x.cpu.cores[i]]), color: COLORS.cpu }], 100, true]),
      live: (s) => ({ "s-cpu": pct(s.cpu.pct), "s-mhz": s.cpu.mhz ? (s.cpu.mhz / 1000).toFixed(2) + " GHz" : "—",
                      "s-ctemp": s.cpu.temp_c != null ? Math.round(s.cpu.temp_c) + " °C" : "—", "s-load": f1(s.cpu.load1), "s-up": uptime(sys.info.uptime_s) }),
    },
    mem: {
      title: "Memory", sub: (s) => gb(s.mem.total_mb) + " RAM",
      html: () => chartBox("c-mem", "Memory usage", "", "big") +
        `<div class="chart-head"><span>Memory composition</span></div><div class="membar" id="membar"></div>
         <div class="legend"><span><i style="background:${COLORS.mem}"></i>In use</span><span><i style="background:${COLORS.mem};opacity:.35"></i>Cached</span><span><i class="free"></i>Free</span></div>
         <div class="stats">${stat("In use", "", "s-used")}${stat("Available", "", "s-avail")}${stat("Cached", "", "s-cached")}${stat("Swap used", "", "s-swap")}${stat("Total", "", "s-total")}</div>`,
      charts: (s) => [["c-mem", [{ pts: pts(SERIES.mem), color: COLORS.mem }], s.mem.total_mb]],
      live: (s) => {
        const m = s.mem, cached = Math.max(0, Math.min(m.cached_mb, m.total_mb - m.used_mb));
        $("#membar").innerHTML = `<i style="width:${(m.used_mb / m.total_mb) * 100}%;background:${COLORS.mem}"></i><i style="width:${(cached / m.total_mb) * 100}%;background:${COLORS.mem};opacity:.35"></i>`;
        $("#c-mem-max").textContent = gb(m.total_mb);
        return { "s-used": `${gb(m.used_mb)} (${pct(m.pct)})`, "s-avail": gb(m.available_mb), "s-cached": gb(m.cached_mb),
                 "s-swap": m.swap_total_mb ? `${gb(m.swap_used_mb)} of ${gb(m.swap_total_mb)}` : "no swap", "s-total": gb(m.total_mb) };
      },
    },
    disk: {
      title: "Disk", sub: (s) => (s.disk.total_gb != null ? `${s.disk.total_gb} GB data drive` : ""),
      html: () => chartBox("c-diskbusy", "Active time", "100%") + chartBox("c-diskio", "Disk transfer rate", "") +
        `<div class="legend"><span><i style="background:${COLORS.disk}"></i>Read</span><span><i class="dash" style="border-color:${COLORS.disk}"></i>Write</span></div>
         <div class="stats">${stat("Active time", "", "s-busy")}${stat("Read speed", "", "s-read")}${stat("Write speed", "", "s-write")}${stat("Used", "", "s-dused")}${stat("Free", "", "s-dfree")}${stat("Capacity", "", "s-dtotal")}</div>`,
      charts: () => { const mx = windowMax([SERIES.diskR, SERIES.diskW], 1e5); $("#c-diskio-max").textContent = rate(mx);
        return [["c-diskbusy", [{ pts: pts(SERIES.diskBusy), color: COLORS.disk }], 100],
                ["c-diskio", [{ pts: pts(SERIES.diskR), color: COLORS.disk }, { pts: pts(SERIES.diskW), color: COLORS.disk, dash: true }], mx]]; },
      live: (s) => ({ "s-busy": pct(s.disk.busy), "s-read": rate(s.disk.read), "s-write": rate(s.disk.write),
                      "s-dused": s.disk.used_gb != null ? `${s.disk.used_gb} GB (${pct(s.disk.pct)})` : "—", "s-dfree": s.disk.free_gb != null ? s.disk.free_gb + " GB" : "—",
                      "s-dtotal": s.disk.total_gb != null ? s.disk.total_gb + " GB" : "—" }),
    },
    net: {
      title: "Network", sub: (s) => Object.keys(s.net.ifaces).join(", ") || "no adapters",
      key: () => Object.keys((last() || { net: { ifaces: {} } }).net.ifaces).join(",") + sys.nic,
      html: (s) => `<div class="pane-tools"><select id="nic" aria-label="Adapter"><option value="">All adapters</option>` +
        Object.keys(s.net.ifaces).map((n) => `<option value="${esc(n)}" ${sys.nic === n ? "selected" : ""}>${esc(n)}</option>`).join("") + `</select></div>` +
        chartBox("c-net", "Throughput", "", "big") +
        `<div class="legend"><span><i style="background:${COLORS.net}"></i>Receive</span><span><i class="dash" style="border-color:${COLORS.net}"></i>Send</span></div>
         <div class="stats">${stat("Send", "", "s-tx")}${stat("Receive", "", "s-rx")}${stat("Adapter", "", "s-nic")}</div><div id="nic-list" class="nic-list"></div>`,
      after: () => ($("#nic").onchange = (e) => { sys.nic = e.target.value; $("#perf-pane")._key = ""; render(); }),
      charts: () => { const mx = windowMax([SERIES.netRx, SERIES.netTx], 12500); $("#c-net-max").textContent = bits(mx);
        return [["c-net", [{ pts: pts(SERIES.netRx), color: COLORS.net }, { pts: pts(SERIES.netTx), color: COLORS.net, dash: true }], mx]]; },
      live: (s) => {
        $("#nic-list").innerHTML = Object.entries(s.net.ifaces).map(([n, v]) => `<div><b>${esc(n)}</b><span>↑ ${bits(v.tx)}</span><span>↓ ${bits(v.rx)}</span></div>`).join("");
        return { "s-tx": bits(nicOf(s).tx), "s-rx": bits(nicOf(s).rx), "s-nic": sys.nic || "All adapters" };
      },
    },
    gpu: {
      title: "GPU", sub: (s) => (s.gpu.ok ? s.gpu.name : ""),
      key: () => String(last() && last().gpu.ok),
      html: (s) => !s.gpu.ok ? `<div class="empty"><b>No NVIDIA GPU detected</b>${esc(s.gpu.error || "")}</div>` :
        `<div class="chart-pair">${chartBox("c-gpu", "Utilization", "100%")}${chartBox("c-vram", "Dedicated GPU memory", gb(s.gpu.mem_total_mb))}
           ${chartBox("c-gtemp", "Temperature", "100 °C")}${chartBox("c-gpow", "Power draw", "")}</div>
         <div class="stats">${stat("Utilization", "", "s-gutil")}${stat("GPU memory", "", "s-vram")}${stat("Temperature", "", "s-gtemp")}${stat("Power draw", "", "s-gpow")}${stat("Fan", "", "s-gfan")}</div>`,
      charts: (s) => { if (!s.gpu.ok) return [];
        const pmax = windowMax([SERIES.gpuPower], 50); $("#c-gpow-max").textContent = Math.round(pmax) + " W";
        return [["c-gpu", [{ pts: pts(SERIES.gpu), color: COLORS.gpu }], 100], ["c-vram", [{ pts: pts(SERIES.vram), color: COLORS.gpu }], s.gpu.mem_total_mb],
                ["c-gtemp", [{ pts: pts(SERIES.gpuTemp), color: COLORS.net }], 100], ["c-gpow", [{ pts: pts(SERIES.gpuPower), color: COLORS.alt }], pmax]]; },
      live: (s) => (!s.gpu.ok ? {} : { "s-gutil": pct(s.gpu.util), "s-vram": `${gb(s.gpu.mem_used_mb).replace(" GB", "")}/${gb(s.gpu.mem_total_mb)} (${pct(s.gpu.mem_pct)})`,
        "s-gtemp": s.gpu.temp_c != null ? Math.round(s.gpu.temp_c) + " °C" : "—", "s-gpow": s.gpu.power_w != null ? f1(s.gpu.power_w) + " W" : "—",
        "s-gfan": s.gpu.fan_pct != null ? pct(s.gpu.fan_pct) : "—" }),
    },
  };

  function renderPane() {
    const s = last(), pane = $("#perf-pane"), p = PANES[sys.sel];
    if (!s) { setHTML(pane, `<div class="empty"><b>Collecting data…</b>The first samples arrive in a few seconds.</div>`); return; }
    const key = sys.sel + "|" + (p.key ? p.key() : "");
    if (pane._key !== key) {                         // rebuild only when the layout changes, so canvases persist
      pane.innerHTML = `<div class="pane-head"><h2>${p.title}</h2><span class="muted" id="pane-sub"></span></div>` + p.html(s);
      pane._key = key; pane._html = null;
      if (p.after) p.after();
    }
    $("#pane-sub").textContent = p.sub(s);
    const live = p.live(s);
    for (const [id, v] of Object.entries(live)) { const el = document.getElementById(id); if (el && el.textContent !== String(v)) el.textContent = v; }
    const label = sys.win >= 120 ? sys.win / 60 + " minutes" : sys.win + " seconds";
    $$(".chart-foot span:first-child", pane).forEach((el) => (el.textContent = label));
  }

  function paint() {                                  // canvases only: cheap enough to run many times a second
    const s = last();
    if (!s || state.tab !== "system") return;
    sys.view.grid = css("--line");
    const now = nowS();
    for (const it of ITEMS) {
      const cv = $(`.rail-item[data-id=${it.id}] canvas`);
      if (cv) draw(cv, { series: it.series(), max: it.max(), now, win: 60, thumb: true });
    }
    for (const [id, series, max, thumb] of PANES[sys.sel].charts(s)) {
      const cv = document.getElementById(id);
      if (cv) draw(cv, { series, max, now, win: sys.win, thumb: !!thumb });
    }
  }

  function renderProcs(d) {
    setHTML($("#sys-procs"), (d.processes || []).slice(0, 8).map((p) => `<tr><td><b>${esc(p.name)}</b><div class="muted small mono">${esc(p.cmd || "")}</div></td>
      <td class="num">${f1(p.cpu)}%</td><td class="num">${p.mem_mb >= 1024 ? (p.mem_mb / 1024).toFixed(1) + " GB" : p.mem_mb + " MB"}</td></tr>`).join("") ||
      `<tr><td colspan="3" class="muted">Collecting…</td></tr>`);
    const g = last() && last().gpu;
    setHTML($("#sys-gpu-procs"), (d.gpu_processes || []).map((p) => `<tr><td><b>${esc(p.name)}</b><div class="muted small">pid ${p.pid}</div></td>
      <td class="num">${p.mem_mb >= 1024 ? (p.mem_mb / 1024).toFixed(1) + " GB" : p.mem_mb + " MB"}</td></tr>`).join("") ||
      `<tr><td colspan="2" class="muted">${g && g.ok ? "Nothing is using the GPU right now." : "No NVIDIA GPU detected."}</td></tr>`);
  }

  function render() { renderRail(); renderGauges(); renderPane(); paint(); }

  // ---------------------------------------------------------------- polling + animation
  async function poll() {
    const d = await api(`/api/system?since=${sys.last}`);
    sys.interval = d.interval; sys.history = d.history_s; sys.info = d.info; sys.job = d.job;
    sys.offset = d.now - Date.now() / 1000;
    for (const s of d.samples) if (s.t > sys.last) { sys.samples.push(s); sys.last = s.t; }
    const keep = Math.ceil(sys.history / sys.interval) + 5;
    if (sys.samples.length > keep) sys.samples.splice(0, sys.samples.length - keep);
    $("#sys-sub").textContent = `${d.info.hostname} · ${d.info.os} · kernel ${d.info.kernel}`;
    render(); renderProcs(d);
  }
  function loop(ts) {
    if (state.tab !== "system" || document.hidden) { sys.raf = 0; return; }
    if (!loop.t || ts - loop.t > 66) { loop.t = ts; paint(); }      // ~15 fps is plenty for a scrolling graph
    sys.raf = requestAnimationFrame(loop);
  }
  function ensureRunning() {
    if (!sys.timer) sys.timer = setInterval(() => {
      if (state.tab === "system" && !document.hidden) poll().catch(() => {});
      else if (state.tab !== "system") { clearInterval(sys.timer); sys.timer = null; }
    }, Math.max(1000, sys.interval * 1000));
    if (!sys.raf && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) sys.raf = requestAnimationFrame(loop);
  }
  // ---------------------------------------------------------------- power (restart app / restart / shut down)
  const POWER = {
    "restart-app": { label: "Restart app", icon: "i-refresh", cls: "secondary", title: "Restart the app services?",
      text: "The dashboard, API and n8n restart. The server itself stays on. Back in about 15 seconds.", ok: "Restart app",
      wait: "Restarting the app…", waitText: "The page reloads by itself when it is back." },
    reboot: { label: "Restart", icon: "i-restart", cls: "secondary", title: "Restart the server?",
      text: "The whole machine reboots. The dashboard is unavailable for 1–3 minutes, then everything starts again on its own.", ok: "Restart server",
      wait: "Restarting the server…", waitText: "This usually takes 1–3 minutes. The page reloads by itself when the server is back." },
    shutdown: { label: "Shut down", icon: "i-power", cls: "danger", title: "Shut down the server?",
      text: "The machine powers off. You will need to switch it on again by hand (power button) - the dashboard cannot start it.", ok: "Shut down",
      wait: "Shutting down…", waitText: "" },
  };
  let powerInfo = null;
  async function loadPower() {
    try { powerInfo = await api("/api/system/power"); } catch (e) { powerInfo = { available: false, actions: [], reason: e.message }; }
    const btns = Object.entries(POWER).map(([a, p]) => {
      const ok = powerInfo.actions.includes(a);
      return `<button class="${p.cls}" data-power="${a}" ${ok ? "" : "disabled"} title="${esc(ok ? p.title : powerInfo.reason)}"><svg><use href="#${p.icon}"/></svg>${p.label}</button>`;
    }).join("");
    setHTML($("#sys-power"), `<span class="power-label">Power</span>${btns}` +
      (powerInfo.available ? "" : `<span class="muted small power-hint">${esc(powerInfo.reason)}</span>`));
    $$("#sys-power button[data-power]").forEach((b) => (b.onclick = () => powerAction(b.dataset.power)));
  }
  async function send(action, force) {
    const r = await fetch("/api/system/power", { method: "POST", headers: { "Content-Type": "application/json", "X-API-Key": window.YTM.key() },
                                                 body: JSON.stringify({ action, confirm: action, force }) });
    return { status: r.status, data: await r.json().catch(() => ({})) };
  }
  async function powerAction(action) {
    const p = POWER[action];
    const job = sys.job || (powerInfo && powerInfo.job);
    const busy = job ? ` The worker is busy right now (${job.kind}${job.video_id ? " for video #" + job.video_id : ""}); it will be interrupted and resumes from its last finished step afterwards.` : "";
    if (!(await window.YTM.dialog({ title: p.title, text: p.text + busy, okLabel: p.ok, danger: true }))) return;
    let r = await send(action, !!job);
    if (r.status === 409) {
      if (!(await window.YTM.dialog({ title: "A job just started", text: r.data.detail, okLabel: "Go ahead anyway", danger: true }))) return;
      r = await send(action, true);
    }
    if (r.status !== 200) return toast(r.data.detail || "Request failed", true);
    overlay(action);
  }
  function overlay(action) {
    const p = POWER[action], o = $("#power-overlay");
    $("#power-title").textContent = p.wait; $("#power-text").textContent = p.waitText;
    $("#power-spin").style.display = ""; o.classList.remove("hidden");
    const t0 = Date.now(); let wentDown = false;
    const check = async () => {
      let up = false;
      try { up = (await fetch("/api/health", { headers: { "X-API-Key": window.YTM.key() }, cache: "no-store" })).ok; } catch (e) { up = false; }
      if (!up) wentDown = true;
      const secs = (Date.now() - t0) / 1000;
      if (action === "shutdown") {
        if (wentDown && secs > 8) {
          $("#power-spin").style.display = "none"; $("#power-title").textContent = "The server is off";
          $("#power-text").textContent = "Switch it on with its power button. This page will reconnect when it is back.";
        }
        if (up && wentDown) return location.reload();
      } else if (up && (wentDown || secs > 20) && secs > 6) {
        return location.reload();
      } else if (secs > 300) {
        $("#power-spin").style.display = "none"; $("#power-title").textContent = "Still not back";
        $("#power-text").textContent = "The server has not answered for 5 minutes. Check it directly (screen or SSH).";
      }
      setTimeout(check, 3000);
    };
    setTimeout(check, 3000);
  }

  loaders.system = async () => { await poll(); ensureRunning(); if (!powerInfo) loadPower(); };
  $$("#sys-window button").forEach((b) => (b.onclick = () => {
    sys.win = +b.dataset.w; $$("#sys-window button").forEach((x) => x.classList.toggle("active", x === b)); render();
  }));
  window.addEventListener("resize", () => state.tab === "system" && paint());
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.tab === "system") ensureRunning(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => state.tab === "system" && paint());
})();
window._booted = true;
window.boot();
