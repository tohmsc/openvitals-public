/* Vitals — local WHOOP dashboard. Plain JS + vendored Chart.js, no build step. */
(() => {
  "use strict";

  const root = document.documentElement;
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

  /* ───────── Tokens → canvas-safe colors ───────── */
  const probe = document.createElement("canvas").getContext("2d");
  const tok = (name) => {
    probe.fillStyle = "#000";
    probe.fillStyle = getComputedStyle(root).getPropertyValue(name).trim() || "#000";
    return probe.fillStyle;
  };
  const alpha = (color, a) => {
    probe.fillStyle = color;
    const c = probe.fillStyle;
    if (c[0] === "#") {
      const n = parseInt(c.slice(1), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }
    const [r, g, b] = c.match(/[\d.]+/g);
    return `rgba(${r},${g},${b},${a})`;
  };

  /* ───────── Formatting ───────── */
  const DAY = 864e5;
  const dShort = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  const dLong = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  const dTime = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const nf = (d) => new Intl.NumberFormat(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const n0 = nf(0), n1 = nf(1);
  const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? "—" : (d ? n1 : n0).format(v));
  // Value with unit; the unit is dropped when there is no value ("—", not "—%").
  const fmtU = (v, u, d = 0) => (v == null || Number.isNaN(v) ? "—" : `${fmt(v, d)}${u}`);

  function ago(ms) {
    const s = (Date.now() - ms) / 1000;
    const steps = [[60, "second"], [3600, "minute", 60], [86400, "hour", 3600], [86400 * 30, "day", 86400], [86400 * 365, "month", 86400 * 30], [Infinity, "year", 86400 * 365]];
    for (const [lim, unit, div = 1] of steps) if (Math.abs(s) < lim) return rtf.format(-Math.round(s / div), unit);
  }
  const hm = (ms) => {
    if (ms == null) return "—";
    const m = Math.round(ms / 6e4);
    return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  };
  const durMin = (ms) => {
    const m = Math.round(ms / 6e4);
    return m >= 60 ? (m % 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m / 60}h`) : `${m}m`;
  };
  const day = (iso) => new Date(iso);
  const titleCase = (s) => (s || "Activity").replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  /* ───────── Stats ───────── */
  const nums = (a) => a.filter((v) => v != null && !Number.isNaN(v));
  const mean = (a) => (a = nums(a)).length ? a.reduce((s, v) => s + v, 0) / a.length : null;
  const sd = (a) => {
    a = nums(a);
    if (a.length < 2) return null;
    const m = mean(a);
    return Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / (a.length - 1));
  };
  const rolling = (a, w) => a.map((_, i) => mean(a.slice(Math.max(0, i - w + 1), i + 1)));
  function baseline(series) {
    // Baseline = the 30 readings before the latest one.
    const prior = series.slice(Math.max(0, series.length - 31), series.length - 1);
    const m = mean(prior), s = sd(prior);
    return m == null ? null : { mean: m, sd: s ?? 0, n: nums(prior).length };
  }

  const zoneOf = (s) => (s == null ? "none" : s >= 67 ? "good" : s >= 34 ? "fair" : "low");
  const ZONE_LABEL = { good: "High", fair: "Moderate", low: "Low", none: "No score" };

  /* ───────── Chart.js setup ───────── */
  const charts = new Map();
  function mount(id, cfg) {
    charts.get(id)?.destroy();
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    const c = new Chart(canvas, cfg);
    charts.set(id, c);
    return c;
  }
  function emptyState(canvasId, title, hint) {
    charts.get(canvasId)?.destroy();
    charts.delete(canvasId);
    const host = document.getElementById(canvasId)?.parentElement;
    if (!host) return;
    host.querySelector(".empty")?.remove();
    const canvas = host.querySelector("canvas");
    canvas.hidden = true;
    host.append(el("div", "empty", `<div><strong>${esc(title)}</strong>${esc(hint)}</div>`));
  }
  function clearEmpty(canvasId) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    canvas.hidden = false;
    canvas.parentElement.querySelector(".empty")?.remove();
  }

  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const act = chart.tooltip?.getActiveElements?.();
      if (!act?.length) return;
      const x = act[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = tok("--border-strong");
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, top); ctx.lineTo(Math.round(x) + 0.5, bottom); ctx.stroke();
      ctx.restore();
    },
  };
  const band = {
    id: "band",
    beforeDatasetsDraw(chart, _a, o) {
      if (o?.lo == null || o?.hi == null) return;
      const { ctx, chartArea: a, scales: { y } } = chart;
      const top = Math.max(a.top, y.getPixelForValue(o.hi)), bot = Math.min(a.bottom, y.getPixelForValue(o.lo));
      if (bot <= top) return;
      ctx.save();
      ctx.fillStyle = alpha(tok("--text"), 0.06);
      ctx.fillRect(a.left, top, a.right - a.left, bot - top);
      ctx.strokeStyle = alpha(tok("--text"), 0.14);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(a.left, Math.round(top) + 0.5); ctx.lineTo(a.right, Math.round(top) + 0.5);
      ctx.moveTo(a.left, Math.round(bot) - 0.5); ctx.lineTo(a.right, Math.round(bot) - 0.5);
      ctx.stroke();
      ctx.restore();
    },
  };

  function theme() {
    return {
      text: tok("--text"), text2: tok("--text-2"), text3: tok("--text-3"),
      grid: tok("--grid"), surface: tok("--surface"), surface3: tok("--surface-3"), border: tok("--border-strong"),
    };
  }

  function baseOptions({ y = {}, stacked = false, tooltip = {}, plugins = {}, xTicks = 6 } = {}) {
    const t = theme();
    const font = { family: getComputedStyle(document.body).fontFamily, size: 11.5 };
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduceMotion.matches ? false : { duration: 500, easing: "easeOutQuart" },
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 4 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface3, borderColor: t.border, borderWidth: 1,
          titleColor: t.text, bodyColor: t.text2, footerColor: t.text,
          titleFont: { ...font, size: 12, weight: "600" }, bodyFont: { ...font, size: 12 }, footerFont: { ...font, size: 12, weight: "600" },
          padding: 10, cornerRadius: 8, caretSize: 0, boxWidth: 8, boxHeight: 8, boxPadding: 4,
          usePointStyle: true, displayColors: true,
          ...tooltip,
        },
        ...plugins,
      },
      scales: {
        x: {
          stacked,
          grid: { display: false },
          border: { color: t.grid },
          ticks: { color: t.text3, font, maxTicksLimit: xTicks, maxRotation: 0, autoSkipPadding: 16 },
        },
        y: {
          stacked,
          position: "right",
          grid: { color: t.grid, drawTicks: false },
          border: { display: false },
          ticks: { color: t.text3, font, padding: 8, maxTicksLimit: 5 },
          ...y,
        },
      },
    };
  }

  /* ───────── State ───────── */
  let DATA = null;
  let RANGE = 30;
  let woExpanded = false;

  const inRange = (rows, key) => {
    if (!rows.length) return rows;
    const last = Math.max(...rows.map((r) => +day(r[key])));
    const cut = last - RANGE * DAY;
    return rows.filter((r) => +day(r[key]) > cut);
  };

  /* ───────── Theme toggle ───────── */
  const themeBtn = $("#theme-toggle");
  const currentTheme = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  function syncThemeButton() {
    themeBtn.setAttribute("aria-label", currentTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme");
  }
  if (!root.dataset.theme && matchMedia("(prefers-color-scheme: light)").matches) root.dataset.theme = "light";
  syncThemeButton();
  themeBtn.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("vitals-theme", next);
    syncThemeButton();
    if (DATA) renderAll();
    renderLive(lastLive);
  });

  /* ───────── Top bar / header ───────── */
  function renderHeader(d) {
    const name = d.profile?.first_name;
    $("#patient").textContent = name ? `${name} · WHOOP 4.0` : "WHOOP 4.0";
    const latest = d.recovery[0]?.created_at;
    if (latest) {
      const t = +day(latest);
      $("#as-of").textContent = `Latest score ${dLong.format(t)} · ${ago(t)}`;
    }
    const sync = $("#sync-status");
    const parts = [];
    if (d.sync?.ble) parts.push(`Strap synced ${ago(d.sync.ble * 1000)}`);
    if (latest) parts.push(`Cloud data through ${dShort.format(+day(latest))}`);
    sync.textContent = parts.join(" · ") || "Not synced yet";
    const stale = latest && Date.now() - +day(latest) > 3 * DAY;
    sync.toggleAttribute("data-stale", !!stale);
    sync.title = stale ? "No new cloud scores in over 3 days. Run whoop_api.py sync to refresh." : "";
  }

  /* ───────── Recovery hero ───────── */
  function renderRecovery(d) {
    const r = d.recovery[0];
    const ring = $("#rec-ring"), status = $("#rec-status");
    const z = zoneOf(r?.recovery_score);
    ring.dataset.zone = z;
    status.dataset.zone = z;
    $("#rec-value").textContent = fmt(r?.recovery_score);
    const v = $(".ring__value", ring);
    v.setAttribute("stroke-dasharray", "0 100");
    requestAnimationFrame(() => requestAnimationFrame(() =>
      v.setAttribute("stroke-dasharray", `${Math.max(0.5, r?.recovery_score ?? 0)} 100`)));
    $(".status__text", status).textContent = r ? `${ZONE_LABEL[z]} recovery` : "No score yet";
    $("#rec-date").textContent = r ? dLong.format(+day(r.created_at)) : "";
    ring.setAttribute("role", "img");
    ring.setAttribute("aria-label", r ? `Recovery ${fmt(r.recovery_score)} percent, ${ZONE_LABEL[z].toLowerCase()}` : "No recovery score");
    const s = d.sleep[0];
    $("#rec-drivers").innerHTML = [
      ["HRV", fmt(r?.hrv_rmssd_ms), "ms"],
      ["Resting HR", fmt(r?.resting_hr), "bpm"],
      ["Sleep", fmt(s?.sleep_perf), "%"],
    ].map(([k, v, u]) => `<div><dt>${k}</dt><dd>${v}<small>${u}</small></dd></div>`).join("");
  }

  /* ───────── Vitals tiles ───────── */
  const VITALS = [
    { key: "hrv_rmssd_ms", src: "recovery", label: "HRV", unit: "ms", d: 0, color: "--m-hrv" },
    { key: "resting_hr", src: "recovery", label: "Resting heart rate", unit: "bpm", d: 0, color: "--m-rhr" },
    { key: "spo2", src: "recovery", label: "Blood oxygen", unit: "%", d: 1, color: "--m-spo2" },
    { key: "skin_temp_c", src: "recovery", label: "Skin temperature", unit: "°C", d: 1, color: "--m-temp" },
    { key: "resp_rate", src: "sleep", label: "Respiratory rate", unit: "rpm", d: 1, color: "--m-resp" },
  ];
  function renderVitals(d) {
    const host = $("#vitals");
    host.innerHTML = "";
    for (const m of VITALS) {
      const rows = d[m.src].slice().reverse();
      const series = rows.map((r) => r[m.key]);
      const latest = series.at(-1);
      const b = baseline(series);
      const card = el("article", "card vital");
      const id = `spark-${m.key}`;
      let deltaHtml = `<span class="vital__delta">Not enough history yet</span>`;
      let rangeTxt = "";
      if (b && latest != null) {
        const z = b.sd ? (latest - b.mean) / b.sd : 0;
        const flag = z > 1 ? "up" : z < -1 ? "down" : "info";
        const pct = ((latest - b.mean) / b.mean) * 100;
        const word = flag === "up" ? "Above your usual range" : flag === "down" ? "Below your usual range" : "Within your usual range";
        const sign = pct > 0 ? "+" : pct < 0 ? "−" : "±";
        deltaHtml = `<p class="vital__delta" data-zone="${flag}"><span class="status__icon" aria-hidden="true"></span>${word}</p>`;
        rangeTxt = `${sign}${n1.format(Math.abs(pct))}% vs 30-day avg ${fmt(b.mean, m.d)} ${m.unit}`;
      }
      card.innerHTML = `
        <h3 class="vital__label"><span class="swatch" style="--c:var(${m.color})" aria-hidden="true"></span>${m.label}</h3>
        <p class="vital__value"><span class="vital__num">${fmt(latest, m.d)}</span><span class="vital__unit">${m.unit}</span></p>
        ${deltaHtml}
        <div class="vital__spark"><canvas id="${id}" role="img" aria-label="${m.label}, last 30 readings"></canvas></div>
        <p class="vital__range">${rangeTxt}</p>`;
      host.append(card);
      const last30 = rows.slice(-30);
      if (last30.length < 2) continue;
      const color = tok(m.color);
      const opts = baseOptions({ plugins: { band: b ? { lo: b.mean - b.sd, hi: b.mean + b.sd } : {} } });
      opts.scales.x.display = false;
      opts.scales.y.display = false;
      opts.layout.padding = { top: 4, bottom: 4, left: 2, right: 2 };
      opts.plugins.tooltip.callbacks = {
        title: (i) => dShort.format(+day(last30[i[0].dataIndex][m.src === "sleep" ? "end" : "created_at"])),
        label: (i) => ` ${fmt(i.parsed.y, m.d)} ${m.unit}`,
      };
      mount(id, {
        type: "line",
        data: {
          labels: last30.map((_, i) => i),
          datasets: [{
            data: last30.map((r) => r[m.key]), borderColor: color, borderWidth: 1.75, cubicInterpolationMode: "monotone",
            pointRadius: (c) => (c.dataIndex === last30.length - 1 ? 3 : 0), pointBackgroundColor: color,
            pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: tok("--surface"), spanGaps: true,
          }],
        },
        options: opts,
        plugins: [band, crosshair],
      });
    }
  }

  /* ───────── Trends ───────── */
  function renderTrends(d) {
    const rec = inRange(d.recovery.slice().reverse(), "created_at");
    const labels = rec.map((r) => dShort.format(+day(r.created_at)));

    // Recovery bars, colored by zone (zone legend + tooltip label carry the meaning too).
    if (rec.length) {
      clearEmpty("c-rec");
      const zc = { good: tok("--good"), fair: tok("--fair"), low: tok("--low") };
      const scores = rec.map((r) => r.recovery_score);
      const counts = { good: 0, fair: 0, low: 0 };
      scores.forEach((s) => counts[zoneOf(s)]++);
      $("#t-rec-stat").innerHTML = `Average <strong>${fmt(mean(scores))}%</strong> · ${counts.good} high, ${counts.fair} moderate, ${counts.low} low`;
      const o = baseOptions({ y: { min: 0, max: 100, ticks: { ...baseOptions().scales.y.ticks, stepSize: 25, callback: (v) => `${v}%` } } });
      o.plugins.tooltip.callbacks = {
        label: (i) => ` ${fmt(i.parsed.y)}% · ${ZONE_LABEL[zoneOf(i.parsed.y)]}`,
      };
      o.plugins.tooltip.displayColors = true;
      const c = mount("c-rec", {
        type: "bar",
        data: { labels, datasets: [{ data: scores, backgroundColor: scores.map((s) => zc[zoneOf(s)]), borderRadius: 3, borderSkipped: "start", categoryPercentage: 0.82, barPercentage: 0.9, maxBarThickness: 14 }] },
        options: o,
      });
      c.canvas.setAttribute("aria-label", `Recovery over the last ${RANGE} days: average ${fmt(mean(scores))} percent; ${counts.good} high, ${counts.fair} moderate and ${counts.low} low days.`);
    } else emptyState("c-rec", "No recovery scores", "Run whoop_api.py sync to pull your history.");

    lineTrend("c-hrv", "#t-hrv-stat", rec, "hrv_rmssd_ms", "--m-hrv", "ms", "Heart rate variability", d.recovery);
    lineTrend("c-rhr", "#t-rhr-stat", rec, "resting_hr", "--m-rhr", "bpm", "Resting heart rate", d.recovery);
  }

  function lineTrend(id, statSel, rows, key, colorVar, unit, name, all) {
    const vals = rows.map((r) => r[key]);
    if (nums(vals).length < 2) return emptyState(id, `No ${name.toLowerCase()} data`, "It appears after a few scored nights.");
    clearEmpty(id);
    const color = tok(colorVar);
    // Baseline over the range shown, so the band matches what you see.
    const m = mean(vals), s = sd(vals) ?? 0;
    const avg7 = rolling(vals, 7);
    $(statSel).innerHTML = `Average <strong>${fmt(m)} ${unit}</strong> · range ${fmt(Math.min(...nums(vals)))}–${fmt(Math.max(...nums(vals)))} ${unit}`;
    const o = baseOptions({ plugins: { band: { lo: m - s, hi: m + s } } });
    const lo = Math.min(...nums(vals), m - s), hi = Math.max(...nums(vals), m + s), pad = (hi - lo) * 0.12;
    o.scales.y.min = Math.max(0, Math.floor((lo - pad) / 5) * 5);
    o.scales.y.max = Math.ceil((hi + pad) / 5) * 5;
    o.plugins.tooltip.callbacks = {
      label: (i) => ` ${i.dataset.label}: ${fmt(i.parsed.y)} ${unit}`,
    };
    const c = mount(id, {
      type: "line",
      data: {
        labels: rows.map((r) => dShort.format(+day(r.created_at))),
        datasets: [
          { label: "Daily", data: vals, borderColor: alpha(color, 0.4), backgroundColor: alpha(color, 0.4), borderWidth: 1.25, cubicInterpolationMode: "monotone", pointRadius: 0, pointHoverRadius: 3, spanGaps: true, pointStyle: "circle" },
          { label: "7-day average", data: avg7, borderColor: color, backgroundColor: color, borderWidth: 2.25, cubicInterpolationMode: "monotone", pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: tok("--surface"), spanGaps: true, pointStyle: "circle" },
        ],
      },
      options: o,
      plugins: [band, crosshair],
    });
    c.canvas.setAttribute("aria-label", `${name} over the last ${RANGE} days: average ${fmt(m)} ${unit}, from ${fmt(Math.min(...nums(vals)))} to ${fmt(Math.max(...nums(vals)))} ${unit}. Thin line is daily, thick line is the 7-day average.`);
  }

  /* ───────── Sleep ───────── */
  const STAGES = [
    { key: "total_sws_ms", label: "Deep", color: "--stage-deep" },
    { key: "total_rem_ms", label: "REM", color: "--stage-rem" },
    { key: "total_light_ms", label: "Light", color: "--stage-light" },
    { key: "total_awake_ms", label: "Awake", color: "--stage-awake" },
  ];
  function renderLastNight(d) {
    const s = d.sleep[0];
    const host = $("#lastnight");
    if (!s) { host.innerHTML = `<div class="empty"><div><strong>No sleep recorded</strong>Wear the strap overnight, then sync.</div></div>`; return; }
    $("#ln-date").textContent = `${dTime.format(+day(s.start))} – ${dTime.format(+day(s.end))}`;
    const asleep = (s.total_in_bed_ms || 0) - (s.total_awake_ms || 0);
    const total = STAGES.reduce((t, st) => t + (s[st.key] || 0), 0) || 1;
    host.innerHTML = `
      <div>
        <p class="headline" aria-label="${hm(asleep)} asleep"><span class="headline__num">${Math.floor(asleep / 36e5)}</span><span class="headline__unit">h</span><span class="headline__num">${Math.round((asleep % 36e5) / 6e4)}</span><span class="headline__unit">m</span></p>
        <p class="headline__label">Time asleep${s.sleep_perf == null ? "" : ` · ${fmt(s.sleep_perf)}% of sleep needed`}</p>
      </div>
      <div>
        <div class="stagebar" role="img" aria-label="${STAGES.map((st) => `${st.label} ${hm(s[st.key])}`).join(", ")}">
          ${STAGES.map((st) => `<span style="--c:var(${st.color});flex:${s[st.key] || 0}"></span>`).join("")}
        </div>
      </div>
      <ul class="stagelist">
        ${STAGES.map((st) => `<li><span class="swatch" style="--c:var(${st.color})"></span><span>${st.label}</span><span class="v">${hm(s[st.key])}</span><span class="p">${n0.format(((s[st.key] || 0) / total) * 100)}%</span></li>`).join("")}
      </ul>
      <dl class="kv kv--grid">
        <div><dt>Efficiency</dt><dd>${fmtU(s.sleep_efficiency, "%")}</dd></div>
        <div><dt>Consistency</dt><dd>${fmtU(s.sleep_consistency, "%")}</dd></div>
        <div><dt>Respiratory rate</dt><dd>${fmtU(s.resp_rate, " rpm", 1)}</dd></div>
        <div><dt>Disturbances</dt><dd>${fmt(s.disturbances)}</dd></div>
      </dl>`;
  }

  function renderSleepChart(d) {
    const rows = inRange(d.sleep.slice().reverse(), "end");
    if (!rows.length) return emptyState("c-sleep", "No sleep data", "Sleep appears here after your first synced night.");
    clearEmpty("c-sleep");
    const h = (ms) => (ms || 0) / 36e5;
    const asleep = rows.map((s) => h(s.total_in_bed_ms) - h(s.total_awake_ms));
    $("#sh-stat").innerHTML = `Average <strong>${hm(mean(asleep) * 36e5)}</strong> asleep per night · ${rows.length} nights`;
    const surface = tok("--surface");
    const o = baseOptions({ stacked: true, y: { suggestedMax: 10, ticks: { ...baseOptions().scales.y.ticks, stepSize: 2, callback: (v) => `${v}h` } } });
    o.plugins.tooltip.callbacks = {
      label: (i) => ` ${i.dataset.label}: ${hm(i.parsed.y * 36e5)}`,
      footer: (items) => `Asleep ${hm(asleep[items[0].dataIndex] * 36e5)}`,
    };
    o.plugins.tooltip.itemSort = (a, b) => b.datasetIndex - a.datasetIndex;
    const c = mount("c-sleep", {
      type: "bar",
      data: {
        labels: rows.map((s) => dShort.format(+day(s.end))),
        datasets: STAGES.map((st, i) => ({
          label: st.label, data: rows.map((s) => h(s[st.key])), backgroundColor: tok(st.color),
          borderColor: surface, borderWidth: { top: i === STAGES.length - 1 ? 0 : 1.5, bottom: 0, left: 0, right: 0 },
          borderRadius: i === STAGES.length - 1 ? { topLeft: 3, topRight: 3 } : 0, borderSkipped: false,
          categoryPercentage: 0.82, barPercentage: 0.9, maxBarThickness: 14,
        })),
      },
      options: o,
    });
    c.canvas.setAttribute("aria-label", `Sleep stages per night over the last ${RANGE} days, average ${hm(mean(asleep) * 36e5)} asleep.`);
  }

  /* ───────── Activity ───────── */
  function renderStrain(d) {
    const rows = inRange(d.cycle.filter((c) => c.end && c.strain > 0).reverse(), "start");
    if (!rows.length) return emptyState("c-strain", "No strain data", "Strain is scored at the end of each day.");
    clearEmpty("c-strain");
    const vals = rows.map((r) => r.strain);
    const peak = rows[vals.indexOf(Math.max(...vals))];
    $("#st-stat").innerHTML = `Average <strong>${fmt(mean(vals), 1)}</strong> · peak ${fmt(peak.strain, 1)} on ${dShort.format(+day(peak.start))}`;
    const o = baseOptions({ y: { min: 0, max: 21, ticks: { ...baseOptions().scales.y.ticks, stepSize: 7 } } });
    o.plugins.tooltip.callbacks = {
      label: (i) => ` Strain ${fmt(i.parsed.y, 1)}`,
      afterLabel: (i) => {
        const r = rows[i.dataIndex];
        return r.avg_hr ? ` Avg ${fmt(r.avg_hr)} bpm · max ${fmt(r.max_hr)} bpm` : "";
      },
    };
    const c = mount("c-strain", {
      type: "bar",
      data: { labels: rows.map((r) => dShort.format(+day(r.start))), datasets: [{ data: vals, backgroundColor: tok("--m-strain"), borderRadius: 3, borderSkipped: "start", categoryPercentage: 0.82, barPercentage: 0.9, maxBarThickness: 14 }] },
      options: o,
    });
    c.canvas.setAttribute("aria-label", `Day strain over the last ${RANGE} days, average ${fmt(mean(vals), 1)} out of 21.`);
  }

  const ZONES = [
    ["zone_zero_milli", "Zone 0", "<50%"], ["zone_one_milli", "Zone 1", "50–60%"], ["zone_two_milli", "Zone 2", "60–70%"],
    ["zone_three_milli", "Zone 3", "70–80%"], ["zone_four_milli", "Zone 4", "80–90%"], ["zone_five_milli", "Zone 5", "90–100%"],
  ];
  function renderZones(d) {
    const inR = inRange(d.workout.slice().reverse(), "start");
    const ws = inR.filter((w) => w.zones);
    const host = $("#zones");
    const tot = ZONES.map(([k]) => ws.reduce((s, w) => s + (w.zones[k] || 0), 0));
    const sum = tot.reduce((a, b) => a + b, 0);
    if (!sum) {
      const [t, h] = inR.length
        ? ["No zone data", `${inR.length} workout${inR.length === 1 ? "" : "s"} in this range, none with heart rate zones.`]
        : ["No workouts in this range", "Try a longer time range."];
      host.innerHTML = `<div class="empty"><div><strong>${t}</strong>${h}</div></div>`;
      $("#hz-stat").textContent = "";
      return;
    }
    const max = Math.max(...tot);
    $("#hz-stat").innerHTML = `<strong>${durMin(sum)}</strong> across ${ws.length} workout${ws.length === 1 ? "" : "s"}`;
    host.innerHTML = ZONES.map(([, name, pct], i) => `
      <div class="zone">
        <span class="zone__name">${name}<small>${pct}</small></span>
        <span class="zone__track" role="img" aria-label="${name}: ${durMin(tot[i])}, ${n0.format((tot[i] / sum) * 100)} percent"><span class="zone__fill" style="--c:var(--zone-${i});width:${(tot[i] / max) * 100}%"></span></span>
        <span class="zone__val">${durMin(tot[i])}</span>
      </div>`).join("");
  }

  function renderWorkouts(d) {
    const all = d.workout;
    const shown = woExpanded ? all : all.slice(0, 8);
    $("#wo-count").textContent = all.length ? `${all.length} recorded` : "";
    const body = $("#wo-body");
    if (!all.length) {
      body.innerHTML = `<tr><td colspan="7"><div class="empty"><div><strong>No workouts yet</strong>Workouts logged in WHOOP show up here after a sync.</div></div></td></tr>`;
      return;
    }
    body.innerHTML = shown.map((w) => {
      const dur = w.end ? +day(w.end) - +day(w.start) : null;
      const name = titleCase(w.sport_name);
      return `<tr>
        <td><span>${dShort.format(+day(w.start))}</span> <span class="muted">${dTime.format(+day(w.start))}</span></td>
        <td><span class="activity"><span class="activity__icon" aria-hidden="true">${esc(name.slice(0, 1))}</span>${esc(name)}</span></td>
        <td class="num">${dur ? durMin(dur) : "—"}</td>
        <td><span class="strainbar"><span class="strainbar__track" aria-hidden="true"><span class="strainbar__fill" style="width:${((w.strain || 0) / 21) * 100}%"></span></span><span class="strainbar__v">${fmt(w.strain, 1)}</span></span></td>
        <td class="num">${fmt(w.avg_hr)} <span class="muted">bpm</span></td>
        <td class="num">${fmt(w.max_hr)} <span class="muted">bpm</span></td>
        <td class="num">${w.kilojoule ? fmt(w.kilojoule / 4.184) : "—"} <span class="muted">kcal</span></td>
      </tr>`;
    }).join("");
    const more = $("#wo-more");
    more.hidden = all.length <= 8;
    more.textContent = woExpanded ? "Show fewer" : `Show all ${all.length} workouts`;
    more.setAttribute("aria-expanded", String(woExpanded));
  }
  $("#wo-more").addEventListener("click", () => { woExpanded = !woExpanded; renderWorkouts(DATA); });

  /* ───────── Device ───────── */
  function renderDevice(d) {
    const soc = d.soc || [];
    const last = soc.at(-1);
    const bat = $("#battery");
    if (last) {
      const z = last.soc_pct < 15 ? "low" : last.soc_pct < 30 ? "fair" : "good";
      bat.dataset.zone = z;
      bat.innerHTML = `<span class="battery__icon" aria-hidden="true"><span class="battery__fill" style="width:${Math.max(4, last.soc_pct)}%"></span></span>
        <span class="battery__num">${fmt(last.soc_pct)}<small>%</small></span>
        ${z === "good" ? "" : `<span class="status" data-zone="${z}"><span class="status__icon" aria-hidden="true"></span>${z === "low" ? "Charge soon" : "Getting low"}</span>`}`;
      if (soc.length > 1) {
        clearEmpty("c-soc");
        const color = tok("--m-battery");
        const o = baseOptions({ y: { min: 0, max: 100, display: false } });
        o.scales.x.display = false;
        o.plugins.tooltip.callbacks = { title: () => "", label: (i) => ` ${fmt(i.parsed.y, 1)}%` };
        o.plugins.tooltip.displayColors = false;
        mount("c-soc", {
          type: "line",
          data: { labels: soc.map((s) => s.rtc), datasets: [{ data: soc.map((s) => s.soc_pct), borderColor: color, borderWidth: 1.75, stepped: false, tension: 0.2, pointRadius: 0, pointHoverRadius: 4, fill: true, backgroundColor: (ctx) => {
            const a = ctx.chart.chartArea; if (!a) return "transparent";
            const g = ctx.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom); g.addColorStop(0, alpha(color, 0.25)); g.addColorStop(1, alpha(color, 0)); return g;
          } }] },
          options: o,
          plugins: [crosshair],
        }).canvas.setAttribute("aria-label", `Battery level across ${soc.length} firmware reports, latest ${fmt(last.soc_pct)} percent.`);
      }
    } else {
      bat.innerHTML = `<div class="empty"><div><strong>No battery reports</strong>Run collector.py with the strap nearby.</div></div>`;
      emptyState("c-soc", "", "");
    }

    const labels = { api_recovery: "Recovery scores", api_sleep: "Sleeps", api_cycle: "Daily cycles", api_workout: "Workouts", ble_hr: "Live HR samples", ble_raw: "Raw strap frames" };
    $("#counts").innerHTML = Object.entries(d.counts).map(([k, v]) => `<div><dt>${labels[k] || k}</dt><dd>${n0.format(v)}</dd></div>`).join("");

    const logs = d.logs || [];
    $("#logs").innerHTML = logs.length
      ? logs.map((l) => `<li><span class="t">${esc(l.rtc)}</span><span class="s">${esc(l.subsystem)}</span><span class="m">${esc(l.message)}</span></li>`).join("")
      : `<li class="empty"><div><strong>No firmware logs decoded</strong>Run decode.py after a strap drain.</div></li>`;
  }

  /* ───────── Live heart rate ───────── */
  let lastLive = null;
  function renderLive(L) {
    lastLive = L;
    const card = $(".card--monitor");
    const pill = $("#live-pill"), pillText = $("#live-pill-text");
    const recent = (L?.recent || []).filter((r) => r.hr);
    const live = !!L?.live;
    card.dataset.live = String(live);
    pill.dataset.state = live ? "live" : "off";
    pillText.textContent = live ? "Live" : "Offline";
    const hr = live ? L.hr : recent.at(-1)?.hr;
    $("#live-hr").textContent = hr ?? "—";
    if (live && hr) card.style.setProperty("--beat", `${(60 / hr).toFixed(2)}s`);
    const lastTs = L?.ts ? L.ts * 1000 : recent.at(-1)?.ts * 1000;
    $("#live-note").textContent = live
      ? "Streaming from the strap"
      : lastTs ? `Last reading ${ago(lastTs)}. Run live.py to stream.` : "No readings yet. Run live.py with the strap nearby.";
    const rr = L?.rr_ms?.[0];
    $("#live-rr").textContent = live && rr ? `${fmt(rr)} ms` : "—";
    const hrs = recent.map((r) => r.hr);
    $("#live-range").textContent = hrs.length ? `${Math.min(...hrs)} / ${Math.max(...hrs)} bpm` : "—";
    $("#live-avg").textContent = hrs.length ? `${fmt(mean(hrs))} bpm` : "—";

    const color = tok("--m-hr");
    const data = recent.map((r) => r.hr);
    const labels = recent.map((r) => dTime.format(r.ts * 1000));
    let c = charts.get("c-live");
    if (c && c.$theme === root.dataset.theme) {
      c.data.labels = labels;
      c.data.datasets[0].data = data;
      c.data.datasets[0].borderColor = live ? color : alpha(color, 0.5);
      c.update("none");
    } else {
      const o = baseOptions({ y: { grace: "15%", ticks: { ...baseOptions().scales.y.ticks, maxTicksLimit: 4 } } });
      o.scales.x.display = false;
      o.scales.y.grid.display = false;
      o.animation = false;
      o.layout.padding = { top: 12, bottom: 6, left: 6, right: 0 };
      o.plugins.tooltip.callbacks = { label: (i) => ` ${i.parsed.y} bpm` };
      o.plugins.tooltip.displayColors = false;
      c = mount("c-live", {
        type: "line",
        data: { labels, datasets: [{ data, borderColor: live ? color : alpha(color, 0.5), borderWidth: 2, tension: 0.35, pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: color, pointHoverBorderColor: tok("--surface"), pointHoverBorderWidth: 2, fill: true,
          backgroundColor: (ctx) => {
            const a = ctx.chart.chartArea; if (!a) return "transparent";
            const g = ctx.chart.ctx.createLinearGradient(0, a.top, 0, a.bottom); g.addColorStop(0, alpha(color, 0.22)); g.addColorStop(1, alpha(color, 0)); return g;
          } }] },
        options: o,
        plugins: [crosshair],
      });
      c.$theme = root.dataset.theme;
    }
    c.canvas.setAttribute("aria-label", hrs.length ? `Heart rate trace, last ${hrs.length} samples, ${Math.min(...hrs)} to ${Math.max(...hrs)} bpm` : "No heart rate samples yet");
  }
  // Announce only connection changes, not every beat.
  const announcer = el("div", "sr-only"); announcer.setAttribute("aria-live", "polite"); document.body.append(announcer);
  let wasLive = null;
  async function pollLive() {
    try {
      const L = await (await fetch("/api/live", { cache: "no-store" })).json();
      if (wasLive !== null && wasLive !== L.live) announcer.textContent = L.live ? `Strap connected, ${L.hr} beats per minute` : "Strap disconnected";
      wasLive = L.live;
      renderLive(L);
    } catch { /* server restarting; next poll retries */ }
  }

  /* ───────── Range control ───────── */
  $("#range").addEventListener("change", (e) => {
    RANGE = +e.target.value;
    renderRanged();
  });
  function renderRanged() {
    renderTrends(DATA);
    renderSleepChart(DATA);
    renderStrain(DATA);
    renderZones(DATA);
  }

  /* ───────── Nav highlight ───────── */
  const links = [...document.querySelectorAll(".nav a")];
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) {
      links.forEach((a) => a.toggleAttribute("aria-current", a.getAttribute("href") === `#${e.target.id}`));
      links.forEach((a) => a.hasAttribute("aria-current") && a.setAttribute("aria-current", "true"));
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  document.querySelectorAll("main .section").forEach((s) => io.observe(s));

  /* ───────── Boot ───────── */
  function renderAll() {
    renderHeader(DATA);
    renderRecovery(DATA);
    renderVitals(DATA);
    renderLastNight(DATA);
    renderWorkouts(DATA);
    renderDevice(DATA);
    renderRanged();
  }

  async function load() {
    const main = $("#main"), err = $("#load-error");
    main.setAttribute("aria-busy", "true");
    err.hidden = true;
    try {
      const r = await fetch("/api/summary", { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      DATA = await r.json();
      main.setAttribute("aria-busy", "false");
      renderAll();
    } catch {
      main.setAttribute("aria-busy", "false");
      err.hidden = false;
      $("#sync-status").textContent = "Offline";
    }
  }
  $("#retry").addEventListener("click", load);

  window.addEventListener("DOMContentLoaded", () => {
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    load();
    pollLive();
    setInterval(pollLive, 2000);
    setInterval(() => DATA && renderHeader(DATA), 60_000);
  });
})();
