/* Vitals — full-screen "Heart & Circulation" view.
   A procedurally generated 3D heart (implicit surface, ray-solved into a lat/long mesh)
   drawn on canvas. It beats on real beat timing from the strap (RR intervals when live,
   otherwise 60/HR), can be dragged to rotate, and has an exploded mode where the mesh
   separates into slices that each carry one metric. No assets, no libraries. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const root = document.documentElement;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const DAY = 864e5;
  const n0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const n1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const fmt = (v, d = 0) => (v == null || Number.isNaN(+v) ? "—" : (d ? n1 : n0).format(v));
  const dDay = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
  const dNum = new Intl.DateTimeFormat(undefined, { day: "numeric" });
  const dMon = new Intl.DateTimeFormat(undefined, { month: "short" });
  const dTime = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const ymd = (t) => { const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  const isToday = () => (window.VITALS?.period || "today") === "today";
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const ago = (ms) => {
    const s = (Date.now() - ms) / 1000;
    if (s < 60) return rtf.format(-Math.round(s), "second");
    if (s < 3600) return rtf.format(-Math.round(s / 60), "minute");
    if (s < 86400) return rtf.format(-Math.round(s / 3600), "hour");
    return rtf.format(-Math.round(s / 86400), "day");
  };

  const stage = $("#hv-stage");
  if (!stage) return;
  const canvas = $("#hv-canvas"), ctx = canvas.getContext("2d");
  const leaders = $("#hv-leaders");
  const calloutHost = $("#hv-callouts");
  const head = $(".hv__head");
  const ecg = $("#hv-ecg"), ectx = ecg.getContext("2d");

  /* ───────── Routing: Heart view ↔ Dashboard ───────── */
  const heartLink = $('.nav a[data-view="heart"]');
  function route() {
    const h = location.hash;
    const heart = !h || h === "#heart";
    document.body.dataset.view = heart ? "heart" : "dash";
    heartLink?.toggleAttribute("aria-current", heart);
    if (heart) {
      heartLink?.setAttribute("aria-current", "true");
      document.querySelectorAll('.nav a:not([data-view])').forEach((a) => a.removeAttribute("aria-current"));
      scrollTo(0, 0);
      requestAnimationFrame(() => { resize(); layoutCallouts(); });
    } else {
      const t = document.querySelector(h);
      if (h === "#overview" || h === "#today") requestAnimationFrame(() => scrollTo(0, 0));
      else if (t) requestAnimationFrame(() => t.scrollIntoView({ block: "start" }));
    }
  }
  addEventListener("hashchange", route);

  /* ───────── Heart geometry ───────── */
  // Taubin's heart surface: (x² + 9/4 y² + z² − 1)³ − x² z³ − 9/80 y² z³ = 0, z up, y depth.
  const F = (x, y, z) => {
    const a = x * x + 2.25 * y * y + z * z - 1;
    return a * a * a - x * x * z * z * z - 0.1125 * y * y * z * z * z;
  };
  const LAT = 30, LON = 96, BANDS = 5;
  const mesh = []; // mesh[i][j] = [x, y, z]
  for (let i = 0; i < LAT; i++) {
    const th = -Math.PI / 2 + ((i + 0.5) / LAT) * Math.PI;
    const row = [];
    for (let j = 0; j < LON; j++) {
      const ph = (j / LON) * Math.PI * 2;
      const d = [Math.cos(th) * Math.cos(ph), Math.cos(th) * Math.sin(ph), Math.sin(th)];
      // Scan inward from outside to find the outer hull, then bisect.
      let r = 1.7;
      while (r > 0 && F(d[0] * r, d[1] * r, d[2] * r) > 0) r -= 0.02;
      let lo = r, hi = r + 0.02;
      for (let k = 0; k < 18; k++) {
        const m = (lo + hi) / 2;
        if (F(d[0] * m, d[1] * m, d[2] * m) > 0) hi = m; else lo = m;
      }
      row.push([d[0] * lo, d[1] * lo, d[2] * lo - 0.08]);
    }
    mesh.push(row);
  }
  const bandOf = (i) => Math.min(BANDS - 1, Math.floor((i / LAT) * BANDS));

  /* ───────── View state ───────── */
  let W = 0, H = 0, DPR = 1;
  let yaw = 0.25, phase = 0, spin = !reduce.matches, dragging = false, lastX = 0, vel = 0;
  let mode = "pulse", explode = 0; // explode animates 0 → 1
  let ink = "20,24,28", glowRGB = "255,72,110", lime = "#d6f53c";
  function readTheme() {
    const cs = getComputedStyle(stage);
    ink = cs.getPropertyValue("--hv-ink").trim() || ink;
    glowRGB = cs.getPropertyValue("--hv-glow").trim() || glowRGB;
    lime = cs.getPropertyValue("--hv-lime").trim() || lime;
  }
  function resize() {
    const r = stage.getBoundingClientRect();
    if (!r.width) return;
    DPR = Math.min(2, devicePixelRatio || 1);
    W = r.width; H = r.height;
    canvas.width = W * DPR; canvas.height = H * DPR;
    const er = ecg.getBoundingClientRect();
    ecg.width = er.width * DPR; ecg.height = er.height * DPR;
    layoutCallouts();
  }
  new ResizeObserver(resize).observe(stage);

  /* ───────── Beat clock (real timing from the strap) ───────── */
  let LIVE = null, hrNow = null, liveNow = false;
  const beats = []; // R-peak times (ms, performance.now clock); last entry is the scheduled next beat
  let rrQueue = [];
  const period = () => {
    const rr = rrQueue.shift();
    if (rr && rr > 280 && rr < 2000) return rr;
    return hrNow ? 60000 / hrNow : null;
  };
  function tickBeats(now) {
    if (!hrNow) { beats.length = 0; return; }
    if (!beats.length) { beats.push(now + 200); return; }
    while (now >= beats.at(-1)) {
      const p = period() || 1000;
      beats.push(beats.at(-1) + p);
      if (beats.length > 14) beats.shift();
      onBeat();
    }
  }
  const lastBeat = (now) => { for (let k = beats.length - 1; k >= 0; k--) if (beats[k] <= now) return beats[k]; return null; };
  // "lub" then "dub": a sharp main contraction and a softer second one ~300 ms later.
  function envelope(now) {
    const b = lastBeat(now);
    if (b == null) return 0;
    const s = (now - b) / 1000;
    return Math.exp(-(((s - 0.035) / 0.05) ** 2)) + 0.55 * Math.exp(-(((s - 0.3) / 0.055) ** 2));
  }
  const pulseDot = $("#hv-pulse-dot");
  function onBeat() {
    if (reduce.matches) return;
    pulseDot.classList.remove("beat"); void pulseDot.offsetWidth; pulseDot.classList.add("beat");
  }

  /* ───────── Data → callouts ───────── */
  let DATA = null, sel = -1; // index into ascending recovery
  let recAsc = [], sleepAsc = [];
  // Two columns on the stage's own edges (same inset as the header and the buttons), two rows each.
  // Order matters for phones, where they sit in a 2×2 grid in this order.
  const CALLOUTS = [
    { key: "hr", side: "left", y: 0.27, band: 2, lon: 0.5 },
    { key: "hrv", side: "right", y: 0.27, band: 4, lon: 0.12 },
    { key: "spo2", side: "right", y: 0.57, band: 1, lon: 0.9 },
    { key: "rhr", side: "left", y: 0.57, band: 3, lon: 0.5 },
  ];
  CALLOUTS.forEach((c) => {
    const n = document.createElement("div");
    n.className = "co";
    n.dataset.side = c.side;
    n.dataset.key = c.key;
    calloutHost.append(n);
    c.node = n;
    c.anchor = [Math.floor(((c.band + 0.5) / BANDS) * LAT), Math.floor(c.lon * LON) % LON];
  });
  // Leader SVG nodes
  leaders.innerHTML = CALLOUTS.map((c) => `<g data-k="${c.key}"><path class="ld__line"/><circle class="ld__halo" r="14"/><circle class="ld__core" r="3"/></g>`).join("");
  CALLOUTS.forEach((c) => { c.g = leaders.querySelector(`g[data-k="${c.key}"]`); });

  function baselineAt(series, i) {
    const prior = series.slice(Math.max(0, i - 30), i).filter((v) => v != null);
    if (prior.length < 5) return null;
    const m = prior.reduce((a, b) => a + b, 0) / prior.length;
    const sd = Math.sqrt(prior.reduce((a, b) => a + (b - m) ** 2, 0) / (prior.length - 1)) || 0;
    return { m, sd };
  }
  function tagFor(series, i, invert = false) {
    const v = series[i], b = baselineAt(series, i);
    if (v == null || !b || !b.sd) return ["—", ""];
    const z = (v - b.m) / b.sd;
    if (Math.abs(z) <= 1) return ["Normal", "ok"];
    const high = z > 1;
    return [high ? "Above usual" : "Below usual", (high !== invert) ? "ok" : "warn"];
  }

  function renderCallouts() {
    if (!DATA) return;
    const today = isToday();
    stage.dataset.period = today ? "today" : "archive";
    const recent = (LIVE?.recent || []).filter((x) => x.hr);
    const lastTs = LIVE?.ts ? LIVE.ts * 1000 : recent.at(-1)?.ts * 1000;
    const hrTag = liveNow ? ["Live", "live"] : [lastTs ? `Last reading ${ago(lastTs)}` : "No signal", ""];
    let content;
    const r = recAsc[sel];
    if (today) {
      const T = window.VITALS?.today, B = window.VITALS?.baseline;
      const vs = (b, unit) => (b?.mean ? `Usual ${fmt(b.mean)} ${unit}` : "No baseline yet");
      content = {
        hr: [fmt(hrNow), "bpm", "Heart rate now", hrTag],
        hrv: [fmt(T?.rmssd), "ms", "HRV today", [T?.rmssd ? vs(B?.hrv, "ms at night") : `${T?.nRR || 0} beat intervals so far`, ""]],
        spo2: [fmt(T?.avg), "bpm", "Average today", [T ? vs(B?.avgHr, "bpm") : "No data yet", ""]],
        rhr: [fmt(T?.min), "bpm", "Lowest today", [vs(B?.rhr, "bpm resting"), ""]],
      };
    } else {
      const hrv = recAsc.map((x) => x.hrv_rmssd_ms), rhr = recAsc.map((x) => x.resting_hr), sp = recAsc.map((x) => x.spo2);
      const sIdx = sleepAsc.findIndex((x) => ymd(+new Date(x.end)) === ymd(+new Date(r?.created_at)));
      const sl = sleepAsc[sIdx];
      content = {
        hr: [fmt(sl?.resp_rate, 1), "rpm", "Respiratory rate", ["That night", "arch"]],
        hrv: [fmt(r?.hrv_rmssd_ms), "ms", "Heart rate variability", tagFor(hrv, sel)],
        spo2: [fmt(r?.spo2, 1), "%", "Blood oxygen", tagFor(sp, sel)],
        rhr: [fmt(r?.resting_hr), "bpm", "Resting heart rate", tagFor(rhr, sel, true)],
      };
    }
    for (const c of CALLOUTS) {
      const [v, u, label, [tag, tone]] = content[c.key];
      const html = `<p class="co__label">${label}</p><p class="co__num">${v}<small>${u}</small></p><p class="co__tag" data-tone="${tone}">${esc(tag)}</p>`;
      if (c.node.innerHTML !== html) c.node.innerHTML = html;
    }
    layoutCallouts();

    // Header
    const rd = r && new Date(r.created_at);
    $("#hv-eyebrow").textContent = today
      ? `Today · ${dDay.format(new Date())}`
      : r ? `Archive · ${dDay.format(rd)}, ${rd.getFullYear()}` : "Archive";
    const stale = $("#hv-stale");
    stale.hidden = today;
    stale.textContent = "WHOOP history, not today";

    // Side card: today = time on strap; archive = that day's recovery
    const card = $("#hv-rec"), num = card.querySelector(".hv-card__num");
    if (today) {
      const T = window.VITALS?.today;
      card.dataset.zone = "today";
      card.querySelector(".hv-card__label").textContent = "Recorded today";
      num.innerHTML = T ? `${Math.floor(T.activeMs / 36e5)}<small>h</small> ${Math.round((T.activeMs % 36e5) / 6e4)}<small>m</small>` : "—";
      $("#hv-rec-sub").textContent = T ? `since ${dTime.format(T.first)} · ${n0.format(T.samples.length)} samples` : "No strap data yet today";
      $("#hv-rec-zone").textContent = liveNow ? "Streaming" : "Strap idle";
    } else {
      const score = r?.recovery_score;
      const z = score == null ? "none" : score >= 67 ? "good" : score >= 34 ? "fair" : "low";
      card.dataset.zone = z;
      card.querySelector(".hv-card__label").textContent = "Recovery";
      num.innerHTML = `${fmt(score)}<small>%</small>`;
      const sl = sleepAsc.find((x) => ymd(+new Date(x.end)) === ymd(+new Date(r?.created_at)));
      $("#hv-rec-sub").textContent = sl ? `${fmt(sl.sleep_perf)}% sleep performance` : " ";
      $("#hv-rec-zone").textContent = { good: "High", fair: "Moderate", low: "Low", none: "No score" }[z];
    }
    const total = Object.values(DATA.counts || {}).reduce((a, b) => a + b, 0);
    $("#hv-count").textContent = n0.format(total);
    canvas.setAttribute("aria-label", `Animated heart${hrNow ? ` beating at ${hrNow} beats per minute` : ""}.`);
    renderSleep();
  }

  /* ───────── Last night (sleep.py: movement + heart rate from the strap) ───────── */
  const hm = (s) => { const t = Math.round(s / 60), h = Math.floor(t / 60), m = t % 60; return (h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`).replace(/ /g, "\u00a0"); };
  function renderSleep() {
    const boxes = document.querySelectorAll("[data-sleep]");
    const today = isToday();
    boxes.forEach((b) => { b.hidden = !today; });
    if (!today) return;
    const n = (DATA?.nights || []).find((x) => x.date === ymd(Date.now()));
    let html;
    if (!n) {
      html = `<p class="hv-card__label">Last night</p><p class="sleep__empty">No sleep recorded. Wear the strap to bed and your night shows up here in the morning.</p>`;
    } else {
      const done = n.status === "done";
      const t = Math.round(n.asleep_s / 60);
      let vs = "The rest of the night is still coming in from the strap.";
      if (done && n.usual_s) {
        const d = n.asleep_s - n.usual_s;
        vs = Math.abs(d) < 1200 ? `About your usual ${hm(n.usual_s)}.` : `${hm(Math.abs(d))} ${d > 0 ? "more" : "less"} than your usual ${hm(n.usual_s)}.`;
      } else if (done) vs = "";
      let segs = [];
      try { segs = JSON.parse(n.segments || "[]"); } catch { /* ignore */ }
      const s0 = segs[0]?.[0], s1 = segs.at(-1)?.[1];
      const bar = segs.length && s1 > s0 ? segs.filter((g) => g[2] !== "-").map((g) =>
        `<span data-s="${g[2]}" style="left:${((g[0] - s0) / (s1 - s0) * 100).toFixed(2)}%;width:${((g[1] - g[0]) / (s1 - s0) * 100).toFixed(2)}%"></span>`).join("") : "";
      const at = (x) => `${((x - s0) / (s1 - s0) * 100).toFixed(2)}%`;
      const on = dTime.format(n.onset * 1000), off = dTime.format(n.wake * 1000);
      const woke = !n.wakeups ? "No wake-ups" : n.wakeups === 1 ? "Woke up once" : `Woke up ${n.wakeups} times`;
      const facts = [done ? woke : null, n.rhr ? `Resting ${fmt(n.rhr)} bpm` : null, n.hrv ? `HRV ${fmt(n.hrv)} ms` : null].filter(Boolean).join(" · ");
      html = `<p class="hv-card__label">${done ? "Last night" : "Last night so far"}</p>
<p class="hv-card__num">${Math.floor(t / 60)}<small>h</small> ${t % 60}<small>m</small></p>
<p class="sleep__vs">${esc(vs) || "&nbsp;"}</p>
<div class="sleep__chart" role="img" aria-label="Asleep from ${on} to ${done ? off : "now"}, ${hm(n.awake_s)} awake in between">
  <div class="sleep__strip">${bar}</div>
  <div class="sleep__marks"><span style="left:${at(n.onset)}">${esc(on)}</span><span style="left:${at(n.wake)}">${done ? esc(off) : ""}</span></div>
</div>
<p class="sleep__facts">${esc(facts)}</p>
<p class="sleep__key" aria-hidden="true"><span data-s="s">Asleep</span><span data-s="a">Awake</span></p>`;
    }
    boxes.forEach((b) => { if (b.innerHTML !== html) b.innerHTML = html; });
  }

  /* ───────── Timeline ───────── */
  const tl = $("#hv-timeline");
  function renderTimeline() {
    if (isToday()) {
      const T = window.VITALS?.today;
      tl.setAttribute("role", "img");
      tl.removeAttribute("aria-label");
      if (!T) { tl.innerHTML = `<p class="tl__empty">No strap data yet today</p>`; return; }
      // Up to 90 bars from the first sample to now: 2 minutes each, wider once the day is longer than 3 hours.
      const first = T.first / 1000, t1 = Math.max(T.last / 1000, Date.now() / 1000);
      const step = Math.max(120, Math.ceil((t1 - first) / 90 / 60) * 60), t0 = Math.floor(first / step) * step;
      const n = Math.min(90, Math.ceil((t1 - t0) / step));
      const bins = Array.from({ length: n }, () => []);
      for (const x of T.samples) { const i = Math.floor((x.ts - t0) / step); if (i >= 0 && i < n) bins[i].push(x.hr); }
      const lo = T.min - 5, hi = T.max + 5;
      const every = Math.max(1, Math.round(n / (W < 760 ? 3 : 6)));
      tl.setAttribute("aria-label", `Heart rate today, ${dTime.format(T.first)} to now`);
      tl.innerHTML = bins.map((b, i) => {
        const v = b.length ? b.reduce((a, c) => a + c, 0) / b.length : null;
        // A time label that would run into "now" at the right edge is dropped.
        const lbl = i === n - 1 ? "now" : i % every === 0 && i < n - 1 - every * 1.2 ? dTime.format((t0 + i * step) * 1000) : "";
        return `<span class="tl__day tl__day--min" data-zone="${v == null ? "none" : "hr"}" style="--h:${v == null ? 6 : 12 + ((v - lo) / (hi - lo)) * 88}%"
          ${v ? `title="${dTime.format((t0 + i * step) * 1000)} · ${Math.round(v)} bpm"` : ""}><span class="tl__bar"></span><span class="tl__lbl">${lbl}</span></span>`;
      }).join("");
      return;
    }
    tl.setAttribute("role", "listbox");
    tl.setAttribute("aria-label", "Choose an archive day");
    const end = Math.min(recAsc.length, sel + 8), start = Math.max(0, end - 30);
    const days = recAsc.slice(start, end);
    tl.innerHTML = days.map((r, k) => {
      const i = start + k, s = r.recovery_score, d = new Date(r.created_at);
      const z = s == null ? "none" : s >= 67 ? "good" : s >= 34 ? "fair" : "low";
      const showLabel = k % 5 === 0;
      return `<button type="button" role="option" class="tl__day" data-i="${i}" data-zone="${z}" aria-selected="${i === sel}"
        aria-label="${dDay.format(d)}, recovery ${fmt(s)}%" tabindex="-1" style="--h:${Math.max(8, s || 0)}%">
        <span class="tl__bar"></span><span class="tl__lbl">${showLabel || i === sel ? `${dNum.format(d)}${k === 0 || i === sel ? ` ${dMon.format(d)}` : ""}` : ""}</span></button>`;
    }).join("") + `<button type="button" class="tl__cal" data-cal aria-label="Open calendar"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="4" width="14" height="13" rx="2.5"/><path d="M3 8h14M7 2.5v3M13 2.5v3"/></svg></button>`;
  }
  function select(i) {
    if (isToday() || !recAsc.length) return;
    sel = Math.max(0, Math.min(recAsc.length - 1, i));
    window.VITALS?.setDate(ymd(+new Date(recAsc[sel].created_at)));
  }
  function syncSel() {
    const k = window.VITALS?.date;
    const i = k ? recAsc.findIndex((x) => ymd(+new Date(x.created_at)) === k) : -1;
    sel = i >= 0 ? i : recAsc.length - 1;
  }
  tl.addEventListener("click", (e) => { const b = e.target.closest(".tl__day"); if (b) select(+b.dataset.i); });
  tl.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { select(sel - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { select(sel + 1); e.preventDefault(); }
    if (e.key === "End") { select(recAsc.length - 1); e.preventDefault(); }
  });

  addEventListener("vitals:data", (e) => {
    DATA = e.detail;
    recAsc = DATA.recovery.slice().reverse();
    sleepAsc = DATA.sleep.slice().reverse();
    syncSel();
    renderTimeline();
    renderCallouts();
  });
  addEventListener("vitals:period", () => { if (!DATA) return; syncSel(); renderTimeline(); renderCallouts(); });
  let wasLive = null;
  addEventListener("vitals:live", (e) => {
    LIVE = e.detail;
    liveNow = !!LIVE.live;
    const recent = (LIVE.recent || []).filter((x) => x.hr);
    hrNow = window.VITALS.currentHr(LIVE); // null once the last reading is no longer fresh: the heart stops rather than beat on an old number
    if (liveNow && LIVE.rr_ms?.length) rrQueue = LIVE.rr_ms.slice(0, 4);
    $("#hv-pulse-state").textContent = liveNow ? "Live pulse" : hrNow ? "Offline" : "No signal";
    $("#hv-pulse-meta").textContent = hrNow ? `${hrNow} bpm${liveNow && LIVE.rr_ms?.[0] ? ` · RR ${Math.round(LIVE.rr_ms[0])} ms` : liveNow ? "" : ` · ${ago(recent.at(-1).ts * 1000)}`}` : "";
    stage.dataset.live = String(liveNow);
    if (wasLive !== liveNow) { wasLive = liveNow; }
    renderCallouts();
  });
  addEventListener("vitals:theme", () => { readTheme(); });
  setInterval(() => { if (DATA && isToday()) renderTimeline(); }, 30000);

  /* ───────── Callout layout + leaders ───────── */
  let anchorsPx = {};
  function layoutCallouts() {
    for (const c of CALLOUTS) {
      c.node.style.left = c.side === "left" ? "var(--hv-inset)" : "";
      c.node.style.right = c.side === "right" ? "var(--hv-inset)" : "";
      c.node.style.top = `${c.y * 100}%`;
    }
    const sr = stage.getBoundingClientRect();
    for (const c of CALLOUTS) {
      const nr = c.node.querySelector(".co__num")?.getBoundingClientRect();
      if (!nr) continue;
      const box = c.node.getBoundingClientRect(), y = (nr.top + nr.bottom) / 2 - sr.top;
      c.start = c.side === "right" ? [box.left - sr.left - 12, y] : [box.right - sr.left + 12, y];
    }
  }

  /* ───────── Render loop ───────── */
  const rotate = (p, cy, sy, ex) => {
    // anatomical tilt: apex down-left
    let [x, y, z] = p;
    z += ex;
    const x1 = x * cy - y * sy, y1 = x * sy + y * cy;              // yaw around z
    const tilt = -0.14, ct = Math.cos(tilt), st = Math.sin(tilt);   // roll in screen plane
    const x2 = x1 * ct - z * st, z2 = x1 * st + z * ct;
    const pitch = 0.18, cp = Math.cos(pitch), sp = Math.sin(pitch); // look slightly from above
    const y3 = y1 * cp - z2 * sp, z3 = y1 * sp + z2 * cp;
    return [x2, y3, z3];
  };

  const NB = 7; // depth buckets for batched strokes
  const paths = Array.from({ length: NB }, () => []);
  function draw(now) {
    if (!W) return;
    const env = envelope(now);
    const target = mode === "exploded" ? 1 : 0;
    explode += (target - explode) * (reduce.matches ? 1 : 0.08);
    // The surface is thin front to back, so a full turn shows it edge-on, where it reads as a flat egg.
    // Instead it rocks gently around the front view, and eases back to the front after a drag.
    if (spin && !dragging && Math.abs(vel) < 0.002) {
      const front = Math.round(yaw / (Math.PI * 2)) * Math.PI * 2;
      phase += 0.006;
      yaw += (front - yaw) * 0.015 + 0.45 * 0.006 * Math.cos(phase);
    }
    if (!dragging && Math.abs(vel) > 0.0001) { yaw += vel; vel *= 0.94; }

    const narrow = W < 760;
    const cx = W * 0.5;
    const S0 = narrow ? Math.min(W * 0.135, 52) : Math.min(W * 0.1, H * 0.13);
    // On phones the heart sits in the slot under the header (the callouts leave 170px for it).
    const cy0 = narrow ? head.offsetTop + head.offsetHeight + 24 + S0 * 1.45 : H * 0.46;
    const S = S0 * (1 + (reduce.matches ? 0 : 0.045 * env)) * (1 - 0.12 * explode);
    const cyw = Math.cos(yaw), syw = Math.sin(yaw);
    const proj = (p, i) => {
      const ex = (bandOf(i) - (BANDS - 1) / 2) * 0.38 * explode;
      const [x, y, z] = rotate(p, cyw, syw, ex);
      const k = 3.4 / (3.4 + y);
      return [cx + x * S * k, cy0 - z * S * k, y];
    };

    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // Inner glow, flares on each contraction.
    const gR = S * 1.25;
    const g = ctx.createRadialGradient(cx, cy0, 0, cx, cy0, gR);
    const gi = 0.05 + 0.09 * env;
    g.addColorStop(0, `rgba(${glowRGB},${gi})`);
    g.addColorStop(0.55, `rgba(${glowRGB},${gi * 0.35})`);
    g.addColorStop(1, `rgba(${glowRGB},0)`);
    ctx.fillStyle = g;
    ctx.fillRect(cx - gR, cy0 - gR, gR * 2, gR * 2);

    // Project the mesh once per frame.
    const P = mesh.map((row, i) => row.map((p) => proj(p, i)));
    for (const b of paths) b.length = 0;
    const bucket = (d) => Math.max(0, Math.min(NB - 1, Math.floor(((d + 1.3) / 2.6) * NB)));
    // latitude rings (every row) + meridians (every 6th column)
    for (let i = 0; i < LAT; i++) for (let j = 0; j < LON; j++) {
      const a = P[i][j], b = P[i][(j + 1) % LON];
      paths[bucket((a[2] + b[2]) / 2)].push(a[0], a[1], b[0], b[1]);
    }
    for (let j = 0; j < LON; j += 6) for (let i = 0; i < LAT - 1; i++) {
      if (explode > 0.02 && bandOf(i) !== bandOf(i + 1)) continue;
      const a = P[i][j], b = P[i + 1][j];
      paths[bucket((a[2] + b[2]) / 2)].push(a[0], a[1], b[0], b[1]);
    }
    for (let k = NB - 1; k >= 0; k--) { // back to front
      const seg = paths[k];
      if (!seg.length) continue;
      const front = 1 - k / (NB - 1);
      ctx.strokeStyle = `rgba(${ink},${0.05 + 0.5 * front ** 1.6})`;
      ctx.lineWidth = 0.6 + 0.7 * front;
      ctx.beginPath();
      for (let q = 0; q < seg.length; q += 4) { ctx.moveTo(seg[q], seg[q + 1]); ctx.lineTo(seg[q + 2], seg[q + 3]); }
      ctx.stroke();
    }
    // Node dots on the front face, brightening with the beat.
    ctx.fillStyle = `rgba(${ink},${0.55 + 0.3 * env})`;
    ctx.beginPath();
    for (let i = 0; i < LAT; i += 2) for (let j = 0; j < LON; j += 3) {
      const p = P[i][j];
      if (p[2] > -0.15) continue;
      const r = 0.8 + (-p[2]) * 0.9;
      ctx.moveTo(p[0] + r, p[1]); ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
    }
    ctx.fill();

    // Slice labels in exploded mode.
    if (explode > 0.05) {
      ctx.font = `500 11px ${getComputedStyle(document.body).fontFamily}`;
      ctx.fillStyle = `rgba(${ink},${0.45 * explode})`;
      ctx.textAlign = "right";
      for (let b = 0; b < BANDS; b++) {
        const i = Math.floor(((b + 0.5) / BANDS) * LAT);
        let minX = Infinity, yy = 0;
        for (let j = 0; j < LON; j++) if (P[i][j][0] < minX) { minX = P[i][j][0]; yy = P[i][j][1]; }
        ctx.fillText(`L${b + 1}`, minX - 14, yy + 4);
      }
    }

    // Leaders follow their anchor point on the rotating mesh.
    for (const c of CALLOUTS) {
      if (!c.start) continue;
      const [ai, aj] = c.anchor;
      // pick the most front-facing point on that ring near the callout's side
      let best = null;
      for (let j = 0; j < LON; j += 2) {
        const p = P[ai][j];
        const sideOk = c.side === "right" ? p[0] > cx : p[0] < cx;
        const score = p[2] - (sideOk ? 0.6 : 0) + Math.abs(j - aj) * 0.0005;
        if (!best || score < best.s) best = { p, s: score };
      }
      const [px, py] = best.p;
      const [sx, sy] = c.start;
      const elbow = c.side === "right" ? sx - 28 : sx + 28;
      c.g.querySelector(".ld__line").setAttribute("d", `M${sx},${sy} H${elbow} L${px},${py}`);
      const halo = c.g.querySelector(".ld__halo"), core = c.g.querySelector(".ld__core");
      halo.setAttribute("cx", px); halo.setAttribute("cy", py);
      core.setAttribute("cx", px); core.setAttribute("cy", py);
      halo.setAttribute("r", 6 + 5 * env);
      halo.style.opacity = 0.12 + 0.3 * env;
    }

    drawECG(now);
  }

  /* ───────── Pulse strip (synthesized from beat timing) ───────── */
  const gauss = (x, w) => Math.exp(-((x / w) ** 2));
  function wave(t) {
    let v = 0;
    for (const b of beats) {
      const s = (t - b) / 1000;
      if (s < -0.4 || s > 0.6) continue;
      v += 0.12 * gauss(s + 0.17, 0.028) - 0.1 * gauss(s + 0.028, 0.01) + gauss(s, 0.012)
        - 0.22 * gauss(s - 0.03, 0.013) + 0.26 * gauss(s - 0.26, 0.05);
    }
    return v;
  }
  function drawECG(now) {
    const w = ecg.width, h = ecg.height;
    if (!w) return;
    ectx.setTransform(1, 0, 0, 1, 0, 0);
    ectx.clearRect(0, 0, w, h);
    const span = 3200; // ms shown
    const color = liveNow ? lime : `rgba(${ink},0.35)`;
    ectx.lineWidth = 1.25 * DPR;
    ectx.lineJoin = "round";
    ectx.strokeStyle = color;
    ectx.beginPath();
    const base = h * 0.66, amp = h * 0.55;
    for (let x = 0; x <= w; x += 2) {
      const t = now - span + (x / w) * span;
      const y = base - wave(t) * amp;
      x ? ectx.lineTo(x, y) : ectx.moveTo(x, y);
    }
    ectx.stroke();
    ectx.shadowBlur = 0;
    // write head
    const y = base - wave(now) * amp;
    ectx.fillStyle = color;
    ectx.beginPath(); ectx.arc(w - 3 * DPR, y, 3 * DPR, 0, Math.PI * 2); ectx.fill();
  }

  /* ───────── Interaction ───────── */
  stage.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button, a, .hv__timeline")) return;
    dragging = true; lastX = e.clientX; vel = 0;
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("dragging");
  });
  stage.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX; lastX = e.clientX;
    yaw += dx * 0.008; vel = dx * 0.008;
  });
  const endDrag = () => { dragging = false; stage.classList.remove("dragging"); };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);

  document.querySelectorAll(".hv-mode[data-mode]").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".hv-mode[data-mode]").forEach((o) => o.setAttribute("aria-checked", String(o === b)));
    stage.dataset.mode = mode;
  }));
  const spinBtn = $("#hv-spin");
  spinBtn.setAttribute("aria-pressed", String(spin));
  spinBtn.addEventListener("click", () => { spin = !spin; spinBtn.setAttribute("aria-pressed", String(spin)); });
  $("#hv-theme").addEventListener("click", () => $("#theme-toggle").click());
  $(".side__theme")?.addEventListener("click", () => $("#theme-toggle").click());
  const fullBtn = $("#hv-full");
  fullBtn.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else root.requestFullscreen?.().catch(() => {});
  });
  document.addEventListener("fullscreenchange", () => {
    const fs = !!document.fullscreenElement;
    fullBtn.setAttribute("aria-label", fs ? "Exit full screen" : "Enter full screen");
    root.toggleAttribute("data-fullscreen", fs);
  });
  addEventListener("keydown", (e) => {
    if (document.body.dataset.view !== "heart" || e.target.closest("input, textarea")) return;
    if (e.key === "e" || e.key === "E") document.querySelector(`.hv-mode[data-mode="${mode === "pulse" ? "exploded" : "pulse"}"]`).click();
    if (e.key === "f" || e.key === "F") fullBtn.click();
  });

  function frame(now) {
    tickBeats(now);
    if (document.body.dataset.view === "heart" && !document.hidden) {
      draw(now);
    }
    requestAnimationFrame(frame);
  }

  readTheme();
  route();
  requestAnimationFrame(frame);
})();
