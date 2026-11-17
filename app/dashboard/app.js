// YT AI Manager dashboard - vanilla JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATUSES = ["idea_pending","idea_approved","scripting","script_review","script_approved","producing","final_review","approved","uploading","scheduled","published","rejected","failed"];
const LABEL = { idea_pending: "Idea review", script_review: "Script review", final_review: "Final review" };
const NICE = (s) => String(s || "").replace(/_/g, " ");
const IN_PROGRESS = ["idea_approved", "scripting", "script_approved", "producing", "approved", "uploading"];
let KEY = "";
try { KEY = localStorage.getItem("ytm_key") || ""; } catch (e) {}
const state = { tab: "review", reviewFilter: "all", channels: [], pipeRows: [] };

// ------------------------------------------------------------------ in-app dialogs (replace prompt/confirm; work well on phones)
function dialog({ title, text = "", value = "", input = null, okLabel = "OK", danger = false, placeholder = "" }) {
  return new Promise((resolve) => {
    const m = $("#modal"), inp = $("#modal-input"), area = $("#modal-area"), ok = $("#modal-ok");
    $("#modal-title").textContent = title; $("#modal-text").textContent = text;
    inp.style.display = input === "text" || input === "password" ? "" : "none";
    area.style.display = input === "area" ? "" : "none";
    inp.type = input === "password" ? "password" : "text";
    inp.value = area.value = value; inp.placeholder = area.placeholder = placeholder;
    ok.textContent = okLabel; ok.className = danger ? "danger" : "";
    m.classList.remove("hidden");
    setTimeout(() => (input === "area" ? area : input ? inp : ok).focus(), 30);
    const done = (val) => { m.classList.add("hidden"); $("#modal-form").onsubmit = $("#modal-cancel").onclick = m.onclick = null; resolve(val); };
    $("#modal-form").onsubmit = (e) => { e.preventDefault(); done(input === "area" ? area.value : input ? inp.value : true); };
    $("#modal-cancel").onclick = () => done(input ? null : false);
    m.onclick = (e) => { if (e.target === m) done(input ? null : false); };
    m._cancel = () => done(input ? null : false);
  });
}
const uiConfirm = (title, text, okLabel = "Confirm", danger = false) => dialog({ title, text, okLabel, danger });
const uiPrompt = (title, text, opts = {}) => dialog({ title, text, input: "text", ...opts });

async function askKey() {
  const k = await uiPrompt("API key", "Enter your YTM_API_KEY (from the .env file on the server).", { value: KEY, input: "password", okLabel: "Save" });
  if (k !== null) { KEY = k.trim(); try { localStorage.setItem("ytm_key", KEY); } catch (e) {} refresh(); }
}

let asking = false;
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-API-Key": KEY, ...(opts.headers || {}) } });
  if (res.status === 401) { if (!asking) { asking = true; askKey().finally(() => (asking = false)); } throw new Error("API key required"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}
const post = (p, body = {}) => api(p, { method: "POST", body: JSON.stringify(body) });
const patch = (p, body) => api(p, { method: "PATCH", body: JSON.stringify(body) });
// stable cache key per version of the video, so thumbnails don't re-download on every refresh
const mediaUrl = (id, name, ver = "") => `/media/${id}/${name}?key=${encodeURIComponent(KEY)}&v=${encodeURIComponent(ver || "")}`;

function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), bad ? 6000 : 3500);
}
async function act(fn, okMsg) {
  const btn = document.activeElement && document.activeElement.tagName === "BUTTON" ? document.activeElement : null;
  btn && btn.classList.add("busy");
  try { await fn(); if (okMsg) toast(okMsg); refresh(); } catch (e) { toast(e.message, true); }
  finally { btn && btn.classList.remove("busy"); }
}
// only touch the DOM when the content really changed: no flicker, videos keep playing, nothing jumps under your finger
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";
function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000, f = s < 0;
  const a = Math.abs(s), u = a < 60 ? [a, "s"] : a < 3600 ? [a / 60, "m"] : a < 86400 ? [a / 3600, "h"] : [a / 86400, "d"];
  if (a > 86400 * 6) return fmtDate(iso);
  return f ? `in ${Math.round(u[0])}${u[1]}` : `${Math.round(u[0])}${u[1]} ago`;
}
const toLocalInput = (iso) => { if (!iso) return ""; const d = new Date(iso); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
const fromLocalInput = (v) => v ? new Date(v).toISOString() : null;
const words = (t) => (t || "").trim() ? t.trim().split(/\s+/).length : 0;
const mmss = (s) => { s = Math.round(s || 0); return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s}s`; };

// ------------------------------------------------------------------ health bar
async function loadHealth() {
  try {
    const h = await api("/api/health");
    if (h.version && $("#app-version").textContent !== "v" + h.version) { $("#app-version").textContent = "v" + h.version; $("#app-version").title = "Installed version"; }
    const chips = [];
    const c = (cls, txt, title = "") => chips.push(`<span class="chip ${cls}" title="${esc(title)}">${esc(txt)}</span>`);
    if (h.failed_videos) c("bad", `${h.failed_videos} failed`, "Open Pipeline → failed");
    c(h.worker_alive ? "ok" : "bad", h.current_job ? `Worker: ${h.current_job.kind} #${h.current_job.video_id ?? h.current_job.channel_id}` : (h.worker_alive ? "Worker idle" : "Worker stopped"));
    c(h.ollama.ok && h.ollama.model_present ? "ok" : "bad", `LLM ${h.ollama.model}`);
    if (h.gpu.ok) c(h.gpu.mem_used_mb / h.gpu.mem_total_mb > 0.92 || h.gpu.temp_c > 85 ? "warn" : "ok",
      `${h.gpu.name.replace("NVIDIA GeForce ", "").replace(" with Max-Q Design", " Max-Q")} ${(h.gpu.mem_used_mb / 1024).toFixed(1)}/${(h.gpu.mem_total_mb / 1024).toFixed(0)} GB · ${h.gpu.temp_c}°C`);
    else c("bad", "GPU not detected");
    c(h.nvenc ? "ok" : "warn", h.nvenc ? "NVENC" : "CPU encode");
    c(h.quota.remaining > 3000 ? "ok" : "warn", `Quota ${h.quota.remaining.toLocaleString()} left`, "YouTube API units left today (resets at midnight Pacific)");
    c(h.disk_free_gb > 20 ? "ok" : "warn", `${h.disk_free_gb} GB free`);
    setHTML($("#health"), chips.join(""));
  } catch (e) { setHTML($("#health"), `<span class="chip bad">${esc(e.message)}</span>`); }
}
$("#health").onclick = (e) => { if (e.target.closest(".chip.bad") && /failed/.test(e.target.textContent)) goPipeline("failed"); };

// ------------------------------------------------------------------ review
function renderKpis(st) {
  const v = st.videos, inprog = IN_PROGRESS.reduce((n, s) => n + (v[s] || 0), 0);
  const k = (cls, n, label, click) => `<button class="kpi ${n ? cls : ""}" onclick="${click}"><b>${n}</b><span>${label}</span></button>`;
  setHTML($("#kpis"), [
    k("hot", v.idea_pending || 0, "Ideas", "setReviewFilter('idea_pending')"),
    k("hot", v.script_review || 0, "Scripts", "setReviewFilter('script_review')"),
    k("hot", v.final_review || 0, "Videos to approve", "setReviewFilter('final_review')"),
    k("info", inprog, "In progress", "goPipeline('')"),
    k("ok", (v.scheduled || 0) + (v.published || 0), "Scheduled / live", "goPipeline('scheduled')"),
    k("bad", v.failed || 0, "Failed", "goPipeline('failed')"),
  ].join(""));
  for (const s of ["idea_pending", "script_review", "final_review"]) $(`#review-filter .n[data-n=${s}]`).textContent = v[s] || "";
  $(`#review-filter .n[data-n=all]`).textContent = st.review_total || "";
  $("#badge-review").textContent = st.review_total || "";
  const running = (st.jobs && ((st.jobs.queued || 0) + (st.jobs.running || 0))) || 0;
  $("#badge-jobs").textContent = running || "";
}

async function loadReview() {
  const f = state.reviewFilter;
  const st = f === "all" ? "idea_pending,script_review,final_review" : f;
  const ch = $("#review-channel").value;
  const [rows, stats] = await Promise.all([api(`/api/videos?status=${st}${ch ? "&channel=" + ch : ""}`), api("/api/stats")]);
  renderKpis(stats);
  $("#btn-approve-ideas").style.display = rows.some((r) => r.status === "idea_pending") ? "" : "none";
  const order = { final_review: 0, script_review: 1, idea_pending: 2 };
  rows.sort((a, b) => order[a.status] - order[b.status] || (b.idea_score || 0) - (a.idea_score || 0));
  const emptyMsg = f === "all"
    ? `<b>All caught up</b>Nothing is waiting for you. Run research from the Channels tab, or let n8n's daily research do it.`
    : `<b>Nothing here</b>No ${f === "idea_pending" ? "ideas" : f === "script_review" ? "scripts" : "final videos"} waiting for review.`;
  setHTML($("#review-list"), rows.length ? rows.map(reviewCard).join("") : `<div class="empty">${emptyMsg}</div>`);
}
window.setReviewFilter = (f) => {
  if (state.tab !== "review") switchTab("review");
  $$("#review-filter button").forEach((x) => x.classList.toggle("active", x.dataset.f === f));
  state.reviewFilter = f; loadReview().catch((e) => toast(e.message, true));
};

function reviewCard(v) {
  const head = `<div class="meta"><span class="pill ${v.format}">${v.format}</span><span>${esc(v.channel_name)}</span><span class="st ${v.status}">${LABEL[v.status]}</span></div>`;
  if (v.status === "idea_pending") {
    const sc = Math.max(0, Math.min(100, Number(v.idea_score) || 0));
    return `<div class="card">${head}<h3>${esc(v.topic)}</h3>
      ${v.idea_score ? `<div class="meta"><span>Score ${v.idea_score}</span></div><div class="scorebar"><i style="width:${sc}%"></i></div>` : ""}
      ${v.angle ? `<p><b>Angle:</b> ${esc(v.angle)}</p>` : ""}${v.hook ? `<p class="hook">“${esc(v.hook)}”</p>` : ""}
      ${v.rationale ? `<p class="muted small">${esc(v.rationale)}</p>` : ""}
      <div class="actions"><button onclick="approve(${v.id})">Approve</button><button class="danger" onclick="rejectV(${v.id})">Reject</button></div></div>`;
  }
  if (v.status === "script_review") {
    const w = words(v.script_text);
    return `<div class="card">${head}<h3>${esc(v.title || v.topic)}</h3>
      <p>${esc((v.script_text || "").slice(0, 300))}${(v.script_text || "").length > 300 ? "…" : ""}</p>
      <div class="meta">${w} words · ~${mmss(w / 2.5)} narration</div>
      <div class="actions"><button onclick="openVideo(${v.id})">Review & edit</button><button class="secondary" onclick="approve(${v.id})">Approve</button></div></div>`;
  }
  return `<div class="card ${v.format}">${head}
    <div class="thumb-wrap"><img class="thumb" src="${mediaUrl(v.id, "thumbnail.jpg", v.updated_at)}" alt="" loading="lazy" onerror="this.style.display='none'">${v.duration_sec ? `<span class="dur">${mmss(v.duration_sec)}</span>` : ""}</div>
    <h3>${esc(v.title || v.topic)}</h3>
    <div class="meta">Publishes ${v.scheduled_at ? fmtDate(v.scheduled_at) : "in the next free slot"}</div>
    <div class="actions"><button onclick="openVideo(${v.id})">Watch & approve</button></div></div>`;
}

window.approve = (id, body = {}) => act(() => post(`/api/videos/${id}/approve`, body), "Approved");
window.rejectV = async (id, closeAfter = false) => {
  if (!(await uiConfirm("Reject this item?", "It moves to Rejected and won't be produced or uploaded.", "Reject", true))) return;
  if (closeAfter) closeDrawer();
  act(() => post(`/api/videos/${id}/reject`, { action: "reject" }), "Rejected");
};
window.retryV = (id) => act(() => post(`/api/videos/${id}/retry`), "Retrying");

// ------------------------------------------------------------------ drawer (detail)
window.openVideo = async (id) => {
  let v;
  try { v = await api(`/api/videos/${id}`); } catch (e) { return toast(e.message, true); }
  let proposed = "";
  if (v.status === "final_review" && !v.scheduled_at) {   // show when it would go out before approving
    try { proposed = (await api(`/api/videos/${id}/next-slot`)).scheduled_at; v.scheduled_at = proposed; } catch (e) {}
  }
  const sections = (v.script && v.script.sections) || [];
  const ver = v.updated_at;
  $("#drawer-title").textContent = `#${v.id} · ${v.channel_name}`;
  let html = `<div class="meta"><span class="pill ${v.format}">${v.format}</span><span>${esc(v.channel_name)}</span><span class="st ${v.status}">${esc(NICE(v.status))}</span><span>#${v.id}</span></div>
    <h2>${esc(v.title || v.topic)}</h2><p class="muted">${esc(v.topic)}${v.angle ? " — " + esc(v.angle) : ""}</p>`;
  if (v.error) html += `<h4>Error</h4><div class="err box">${esc(v.error)}</div>`;

  if (v.video_path) {
    html += `<h4>Video</h4><div class="media-row ${v.format === "short" ? "short" : ""}"><video controls playsinline preload="metadata" src="${mediaUrl(v.id, "final.mp4", ver)}"></video>
      <div class="media-side"><img src="${mediaUrl(v.id, "thumbnail.jpg", ver)}" alt="Thumbnail" onerror="this.style.display='none'">
        <span class="muted small">${mmss(v.duration_sec)} · ${v.format}</span>
        <a class="dl" href="${mediaUrl(v.id, "final.mp4", ver)}" download="video-${v.id}.mp4"><svg><use href="#i-download"/></svg>Download video</a>
        <a class="dl" href="${mediaUrl(v.id, "thumbnail.jpg", ver)}" download="thumbnail-${v.id}.jpg"><svg><use href="#i-download"/></svg>Thumbnail</a></div></div>`;
  }
  const editMeta = ["final_review", "approved"].includes(v.status);
  if (v.description || editMeta) {
    const dis = editMeta ? "" : "disabled";
    html += `<h4>Metadata</h4>
      <div class="field"><label for="f-title"><span>Title</span><span id="f-title-n">${(v.title || "").length}/100</span></label><input id="f-title" maxlength="100" value="${esc(v.title)}" ${dis} oninput="$('#f-title-n').textContent=this.value.length+'/100'"></div>
      <div class="field"><label for="f-desc">Description</label><textarea id="f-desc" rows="7" ${dis}>${esc(v.description)}</textarea></div>
      <div class="field"><label for="f-tags">Tags (comma separated)</label><input id="f-tags" value="${esc((v.tags || []).join(", "))}" ${dis}></div>
      <div class="field"><label for="f-sched"><span>Publish at (your local time)</span><span>${proposed ? "next free slot" : ""}</span></label><input id="f-sched" type="datetime-local" value="${toLocalInput(v.scheduled_at)}" data-proposed="${proposed ? toLocalInput(proposed) : ""}" ${dis}></div>`;
  }
  if (sections.length) {
    const editable = v.status === "script_review";
    const dis = editable ? "" : "disabled";
    html += `<h4>Script · ${sections.reduce((n, s) => n + words(s.text), 0)} words · ${sections.length} sections</h4><div id="sections">` +
      sections.map((s, i) => `<div class="section-edit" data-chapter="${esc(s.chapter || "")}">
        ${s.chapter && (i === 0 || sections[i - 1].chapter !== s.chapter) ? `<div class="ch">${esc(s.chapter)}</div>` : ""}
        <textarea rows="3" class="s-text" aria-label="Narration ${i + 1}" ${dis}>${esc(s.text)}</textarea>
        <div><label>Visual search</label><input class="s-visual" aria-label="Visual ${i + 1}" value="${esc(s.visual)}" ${dis}></div></div>`).join("") + `</div>`;
  }
  if (v.assets && v.assets.length) {
    const stock = v.assets.filter((a) => a.kind === "stock_video");
    if (stock.length) html += `<h4>Footage credits (${stock.length})</h4><div class="events">` +
      stock.map((a) => `<div>${esc(a.provider)} · ${esc(a.author || "")} · <a href="${esc(a.source_url)}" target="_blank" rel="noopener" class="muted">${esc(a.license)}</a></div>`).join("") + `</div>`;
  }
  if (v.youtube_video_id) html += `<h4>YouTube</h4><p><a href="https://youtu.be/${esc(v.youtube_video_id)}" target="_blank" rel="noopener">youtu.be/${esc(v.youtube_video_id)} ↗</a>${v.stats ? ` · ${v.stats.views.toLocaleString()} views` : ""}</p>`;
  html += `<h4>History</h4><div class="events">${(v.events || []).map((e) => `<div>${fmtDate(e.created_at)} · <b>${esc(e.action)}</b> ${esc(e.detail || "")} <span class="muted">(${esc(e.actor)})</span></div>`).join("") || "—"}</div>`;

  html += `<div class="sticky-actions">`;
  if (v.status === "script_review") html += `<button onclick="saveScript(${v.id}, true)">Save & approve script</button><button class="secondary" onclick="saveScript(${v.id}, false)">Save edits</button><button class="secondary" onclick="redo(${v.id})">Regenerate with notes…</button><button class="danger" onclick="rejectV(${v.id}, true)">Reject</button>`;
  if (v.status === "final_review") html += `<button onclick="approveFinal(${v.id})">Approve & schedule</button><button class="secondary" onclick="saveMeta(${v.id})">Save metadata</button><button class="secondary" onclick="redo(${v.id})">Re-produce…</button><button class="danger" onclick="rejectV(${v.id}, true)">Reject</button>`;
  if (v.status === "approved") html += `<button class="secondary" onclick="saveMeta(${v.id})">Save metadata</button><span class="muted small">Uploads on the next pipeline tick</span>`;
  if (v.status === "idea_pending") html += `<button onclick="approve(${v.id}); closeDrawer()">Approve idea</button><button class="danger" onclick="rejectV(${v.id}, true)">Reject</button>`;
  if (v.status === "failed") html += `<button onclick="retryV(${v.id}); closeDrawer()">Retry from failed stage</button><button class="danger" onclick="rejectV(${v.id}, true)">Reject</button>`;
  html += `</div>`;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");
  $(".drawer-panel").scrollTop = 0;
  document.body.style.overflow = "hidden";
};
window.closeDrawer = () => {
  const vid = $("#drawer-body video"); if (vid) vid.pause();
  $("#drawer").classList.add("hidden"); $("#drawer-body").innerHTML = ""; document.body.style.overflow = "";
};

function collectSections() {
  return $$("#sections .section-edit").map((el) => ({ text: $(".s-text", el).value, visual: $(".s-visual", el).value, chapter: el.dataset.chapter || null }));
}
function collectMeta() {
  const sched = $("#f-sched");
  // untouched proposal -> let the server pick the slot at approval time (avoids two videos taking the same slot)
  const untouched = sched.dataset.proposed && sched.value === sched.dataset.proposed;
  return { title: $("#f-title").value, description: $("#f-desc").value,
           tags: $("#f-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
           scheduled_at: untouched ? "" : (fromLocalInput(sched.value) || "") };
}
window.saveScript = (id, andApprove) => act(async () => {
  await patch(`/api/videos/${id}`, { sections: collectSections() });
  if (andApprove) { await post(`/api/videos/${id}/approve`); closeDrawer(); }
}, andApprove ? "Script approved - production queued" : "Saved");
window.saveMeta = (id) => act(() => patch(`/api/videos/${id}`, collectMeta()), "Saved");
window.approveFinal = (id) => act(async () => {
  const m = collectMeta();
  await patch(`/api/videos/${id}`, m);
  await post(`/api/videos/${id}/approve`, { scheduled_at: m.scheduled_at || null });
  closeDrawer();
}, "Approved - will upload on the next tick");
window.redo = async (id) => {
  const notes = await dialog({ title: "What should change?", text: "Your notes are fed back to the AI, which then rewrites (or re-produces) this video.",
                               input: "area", okLabel: "Regenerate", placeholder: "e.g. shorter intro, more concrete examples, calmer tone" });
  if (notes === null) return;
  closeDrawer();
  act(() => post(`/api/videos/${id}/reject`, { action: "redo", notes }), "Regenerating");
};

// ------------------------------------------------------------------ pipeline
async function loadPipeline() {
  const st = $("#pipe-status").value, ch = $("#pipe-channel").value;
  const [rows, stats] = await Promise.all([api(`/api/videos?limit=300${st ? "&status=" + st : ""}${ch ? "&channel=" + ch : ""}`), api("/api/stats")]);
  renderKpis(stats);
  setHTML($("#counts"), `<button class="count ${st ? "" : "active"}" onclick="filterStatus('')"><b>${Object.values(stats.videos).reduce((a, b) => a + b, 0)}</b>all</button>` +
    STATUSES.filter((s) => stats.videos[s]).map((s) => `<button class="count ${st === s ? "active" : ""}" onclick="filterStatus('${s}')"><b>${stats.videos[s]}</b><span class="st ${s}">${NICE(s)}</span></button>`).join(""));
  state.pipeRows = rows;
  renderPipeline();
}
function renderPipeline() {
  const q = ($("#pipe-search").value || "").trim().toLowerCase();
  const rows = q ? state.pipeRows.filter((v) => `${v.title || ""} ${v.topic || ""} ${v.id}`.toLowerCase().includes(q)) : state.pipeRows;
  setHTML($("#pipe-body"), rows.map((v) => `<tr class="click" onclick="openVideo(${v.id})">
    <td class="muted hide-m">${v.id}</td><td class="hide-m">${esc(v.channel_name)}</td><td class="hide-m"><span class="pill ${v.format}">${v.format}</span></td>
    <td class="title">${esc(v.title || v.topic)}
      <div class="m-only muted small">#${v.id} · <span class="pill ${v.format}">${v.format}</span> ${esc(v.channel_name)} · ${v.scheduled_at ? "publishes " + fmtDate(v.scheduled_at) : "updated " + ago(v.updated_at)}</div>
      ${v.error ? `<div class="err">${esc(v.error.slice(0, 160))}</div>` : ""}</td>
    <td class="status"><span class="st ${v.status}">${NICE(v.status)}</span></td><td class="muted hide-m">${fmtDate(v.scheduled_at)}</td><td class="muted hide-m" title="${esc(fmtDate(v.updated_at))}">${ago(v.updated_at)}</td>
    <td class="act">${v.status === "failed" ? `<button class="secondary" onclick="event.stopPropagation(); retryV(${v.id})">Retry</button>` : ""}</td></tr>`).join("") ||
    `<tr class="emptyrow"><td colspan="8" class="muted">${q ? "No titles match your search." : "No videos."}</td></tr>`);
}
window.filterStatus = (s) => { $("#pipe-status").value = s; loadPipeline().catch((e) => toast(e.message, true)); };
window.goPipeline = (s) => { switchTab("pipeline", false); filterStatus(s); };

// ------------------------------------------------------------------ channels
async function loadChannels() {
  const rows = await api("/api/channels");
  state.channels = rows;
  for (const sel of ["#review-channel", "#pipe-channel"]) {
    const cur = $(sel).value;
    const html = `<option value="">All channels</option>` + rows.filter((c) => c.active).map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`).join("");
    if ($(sel)._html !== html) { $(sel).innerHTML = html; $(sel)._html = html; $(sel).value = cur; }
  }
  setHTML($("#channel-list"), rows.map((c) => `<div class="card">
    <div class="meta"><span class="chip ${c.active ? "ok" : "warn"}">${c.active ? "active" : "inactive"}</span><span class="chip ${c.token ? "ok" : "bad"}">${c.token ? "YouTube linked" : "not linked"}</span></div>
    <h3>${esc(c.name)} <span class="muted small">(${esc(c.slug)})</span></h3>
    <p class="muted">${esc(c.niche)}</p>
    <div class="meta">${c.youtube_title ? "▶ " + esc(c.youtube_title) + " · " : ""}${c.videos} videos · ${c.uploaded} uploaded</div>
    ${c.token ? "" : `<p class="muted small">Link it on the server: <code>./ytm.sh auth ${esc(c.slug)}</code></p>`}
    <div class="actions"><button class="secondary" onclick="research('${esc(c.slug)}')" ${c.active ? "" : "disabled"}>Run research now</button></div></div>`).join("") ||
    `<div class="empty"><b>No channels yet</b>Add them to config/channels.yaml and press “Reload channels.yaml”.</div>`);
}
window.research = (slug) => act(() => post("/api/research/run", { channel: slug }), "Research queued - ideas appear in Review in a few minutes");

// ------------------------------------------------------------------ analytics
async function loadAnalytics() {
  const s = await api("/api/analytics/summary");
  const max = Math.max(1, ...s.channels.map((c) => c.views));
  setHTML($("#analytics-channels"), s.channels.map((c) => `<div class="card"><h3>${esc(c.name)}</h3>
    <div class="big">${c.views.toLocaleString()} <small>views</small></div>
    <div class="bar"><i style="width:${(c.views / max) * 100}%"></i></div>
    <div class="meta">${c.videos} tracked · ${c.likes.toLocaleString()} likes · ${c.comments.toLocaleString()} comments</div></div>`).join(""));
  setHTML($("#analytics-body"), s.videos.map((v) => `<tr class="click analytics" onclick="openVideo(${v.id})"><td class="title">${esc(v.title)}
      <div class="m-only muted small"><span class="pill ${v.format}">${v.format}</span> ${esc(v.channel)} · <a href="https://youtu.be/${esc(v.youtube_video_id)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">open ↗</a></div></td>
    <td class="hide-m">${esc(v.channel)}</td><td class="hide-m"><span class="pill ${v.format}">${v.format}</span></td>
    <td class="num" data-label="Views">${v.views.toLocaleString()}</td><td class="num" data-label="Likes">${v.likes.toLocaleString()}</td>
    <td class="num" data-label="Comments">${v.comments.toLocaleString()}</td><td class="hide-m"><a href="https://youtu.be/${esc(v.youtube_video_id)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="muted">open ↗</a></td></tr>`).join("") ||
    `<tr class="emptyrow"><td colspan="7" class="muted">No stats yet - they appear after uploads and the nightly analytics sync.</td></tr>`);
}

// ------------------------------------------------------------------ jobs
async function loadJobs() {
  const rows = await api("/api/jobs?limit=150");
  const cls = (s) => s === "failed" ? "failed" : s === "done" ? "published" : s === "cancelled" ? "rejected" : "producing";
  setHTML($("#jobs-body"), rows.map((j) => `<tr><td class="muted hide-m">${j.id}</td><td class="hide-m">${j.kind}</td>
    <td class="title"><span class="m-only">${esc(j.kind)} · </span>${j.video_id ? `<a href="#" onclick="openVideo(${j.video_id});return false">video #${j.video_id}</a>` : esc(j.channel_slug || "all channels")}
      <div class="m-only muted small">job #${j.id} · ${ago(j.created_at)}${j.finished_at ? " · finished " + ago(j.finished_at) : ""}</div></td>
    <td class="status"><span class="st ${cls(j.status)}">${j.status}</span></td>
    <td class="muted hide-m">${fmtDate(j.created_at)}</td><td class="muted hide-m">${fmtDate(j.finished_at)}</td>
    <td class="meta-cell">${j.error ? `<div class="err">${esc(j.error.slice(0, 300))}</div>` : `<span class="muted mono">${esc(j.result_json ? j.result_json.slice(0, 160) : "")}</span>`}</td>
    <td class="act">${j.status === "queued" ? `<button class="secondary" onclick="act(() => post('/api/jobs/${j.id}/cancel'), 'Cancelled')">Cancel</button>` : ""}</td></tr>`).join("") ||
    `<tr class="emptyrow"><td colspan="8" class="muted">No jobs yet.</td></tr>`);
}

// ------------------------------------------------------------------ wiring
const loaders = { review: loadReview, pipeline: loadPipeline, channels: loadChannels, analytics: loadAnalytics, jobs: loadJobs };
let refreshing = false;
async function refresh() {
  if (!$("#modal").classList.contains("hidden")) return;
  if (!$("#drawer").classList.contains("hidden") && document.activeElement && ["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) return;
  if (refreshing) return;
  refreshing = true; $("#btn-refresh").classList.add("spin");
  loadHealth();
  try {
    if (!state.channels.length) await loadChannels();
    await loaders[state.tab]();
    if (!["review", "pipeline"].includes(state.tab)) api("/api/stats").then(renderKpis).catch(() => {});
  } catch (e) { toast(e.message, true); }
  finally { refreshing = false; setTimeout(() => $("#btn-refresh").classList.remove("spin"), 250); }
}
window.act = act; window.post = post; window.$ = $;

function switchTab(t, load = true) {
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === t));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + t));
  state.tab = t; window.scrollTo({ top: 0 });
  try { history.replaceState(null, "", "#" + t); } catch (e) {}
  if (load) refresh();
}
$$("#tabs button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
$$("#review-filter button").forEach((b) => b.onclick = () => setReviewFilter(b.dataset.f));
$("#pipe-status").innerHTML += STATUSES.map((s) => `<option value="${s}">${NICE(s)}</option>`).join("");
$("#review-channel").onchange = () => loadReview().catch((e) => toast(e.message, true));
$("#pipe-status").onchange = $("#pipe-channel").onchange = () => loadPipeline().catch((e) => toast(e.message, true));
$("#pipe-search").oninput = renderPipeline;
$("#btn-key").onclick = askKey;
$("#btn-refresh").onclick = () => { refreshing = false; refresh(); };
$("#btn-tick").onclick = () => act(() => post("/api/pipeline/tick"), "Tick done");
$("#btn-sync").onclick = () => act(async () => { await post("/api/channels/sync"); await loadChannels(); }, "Channels reloaded");
$("#btn-research-all").onclick = () => act(() => post("/api/research/run", {}), "Research queued for all channels");
$("#btn-analytics").onclick = () => act(() => post("/api/analytics/sync"), "Analytics sync queued");
$("#btn-approve-ideas").onclick = async () => {
  const ideaIds = (await api(`/api/videos?status=idea_pending${$("#review-channel").value ? "&channel=" + $("#review-channel").value : ""}`)).map((v) => v.id);
  if (!ideaIds.length || !(await uiConfirm(`Approve ${ideaIds.length} idea(s)?`, "Each approved idea is scripted next by the AI.", "Approve all"))) return;
  act(async () => { for (const id of ideaIds) await post(`/api/videos/${id}/approve`); }, `${ideaIds.length} ideas approved`);
};
$("#drawer-close").onclick = closeDrawer;
$("#drawer").onclick = (e) => { if (e.target.id === "drawer") closeDrawer(); };
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#modal").classList.contains("hidden")) $("#modal")._cancel();
  else closeDrawer();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });   // phone back from background -> fresh data

// system.js (loaded next) registers loaders.system, then calls boot()
window.YTM = { $, $$, esc, api, state, loaders, toast, setHTML, switchTab, dialog, key: () => KEY };
window.boot = () => {
  const startTab = (location.hash || "").slice(1);
  if (loaders[startTab]) switchTab(startTab, false);
  if (!KEY) askKey(); else refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
};
window.addEventListener("load", () => { if (!window._booted) window.boot(); });   // system.js missing/broken: the rest still works
