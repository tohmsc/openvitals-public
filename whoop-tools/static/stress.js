/* Vitals — Today page: stress monitor (stress.py) and health alerts (alerts.py).
   Self-contained: reads DATA.stress and DATA.alerts from the "vitals:data" event and redraws
   on "vitals:period" (app.js fires it after each quiet refresh). */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const dTime = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const n1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const ymd = (t) => { const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const LEVEL_VAR = { Low: "--good", Medium: "--fair", High: "--low" };
  const STATE_WORD = { asleep: "asleep", moving: "moving", active: "exercise", settling: "settling", few_beats: "too few clean beats", no_motion: "waiting for movement data" };
  let DATA = null, chart = null;

  function renderAlerts(list) {
    const card = $("#alerts-card"), host = $("#alerts-list");
    if (!card || !host) return;
    card.hidden = !list.length;
    host.innerHTML = list.map((a) => `<li class="hal">
      <p class="hal__title">${esc(a.title)}</p>
      <p class="hal__text">${esc(a.text)}</p>
      <p class="hal__why">${esc(a.why)}</p>
      <p class="hal__why">${esc(a.threshold)}</p>
    </li>`).join("");
    const note = $("#alerts-note");
    if (note) note.textContent = list[0]?.note || "";
  }

  function renderStress(s) {
    const now = $("#stress-now"), meta = $("#stress-meta"), base = $("#stress-base"), canvas = $("#c-stress");
    if (!now || !canvas) return;
    const today = s && s.date === ymd(Date.now());
    if (!today) {
      now.innerHTML = `<span class="stress__level">No reading</span><span class="stress__why">Nothing from the strap today yet.</span>`;
      meta.textContent = "";
      base.textContent = s?.baseline?.text || "";
      chart?.destroy(); chart = null;
      return;
    }
    const n = s.now;
    if (n.level) {
      now.style.setProperty("--c", `var(${LEVEL_VAR[n.level]})`);
      now.innerHTML = `<span class="stress__dot" aria-hidden="true"></span><span class="stress__level">${n.level}</span>
        <span class="stress__num">${n1.format(n.score)}<small> / 3</small></span>
        <span class="stress__why">as of ${dTime.format(n.ts * 1000)}${n.pending ? ", newer minutes still coming in from the strap" : ""}</span>`;
    } else {
      now.style.removeProperty("--c");
      now.innerHTML = `<span class="stress__level">Can't tell right now</span><span class="stress__why">${esc(n.why)}${n.ts ? ` (${dTime.format(n.ts * 1000)})` : ""}</span>`;
    }
    const scored = s.series.filter((w) => w.score != null);
    const counts = {};
    for (const w of s.series) if (w.state !== "ok" && w.state !== "no_data") counts[w.state] = (counts[w.state] || 0) + 1;
    const skipped = Object.entries(counts).map(([k, v]) => `${STATE_WORD[k] || k} ${v * 5} min`).join(", ");
    meta.textContent = scored.length
      ? `${scored.length * 5} min scored today${skipped ? `. No score while ${skipped}.` : "."}`
      : `No calm, still stretch to score yet today${skipped ? ` (${skipped}).` : "."}`;
    base.textContent = s.baseline?.text || "";

    // 0-3 per 5 minutes, a gap where there is no score; bands at 1 and 2
    const pts = s.series.map((w) => ({ x: w.ts * 1000 + 150000, y: w.score }));
    const t0 = new Date(); t0.setHours(0, 0, 0, 0);
    const first = s.series.find((w) => w.state !== "no_data");
    const x0 = Math.max(+t0, Math.min(first ? first.ts * 1000 : Date.now(), Date.now() - 3 * 36e5));
    const font = { family: getComputedStyle(document.body).fontFamily, size: 11 };
    const color = (v) => (v == null ? css("--text-3") : css(v >= 2 ? "--low" : v >= 1 ? "--fair" : "--good"));
    chart?.destroy();
    chart = new Chart(canvas, {
      type: "line",
      data: { datasets: [{ data: pts, spanGaps: false, borderWidth: 1.5, tension: 0.2, pointRadius: (c) => {
        const d = c.dataset.data, i = c.dataIndex;
        return d[i]?.y != null && d[i - 1]?.y == null && d[i + 1]?.y == null ? 2 : 0;
      }, segment: { borderColor: (c) => color(Math.max(c.p0.parsed.y, c.p1.parsed.y)) },
      pointBackgroundColor: (c) => color(c.raw?.y), borderColor: css("--text-2") }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: { legend: { display: false }, tooltip: {
          backgroundColor: css("--surface"), borderColor: css("--border"), borderWidth: 1, titleColor: css("--text"), bodyColor: css("--text-2"),
          callbacks: { title: (i) => dTime.format(i[0].parsed.x - 150000), label: (i) => {
            const w = s.series[i.dataIndex];
            return w.score != null ? ` ${w.score >= 2 ? "High" : w.score >= 1 ? "Medium" : "Low"} ${n1.format(w.score)} · HR ${Math.round(w.hr)} · HRV ${Math.round(w.rmssd)} ms` : ` No score: ${STATE_WORD[w.state] || "no data"}`;
          } } } },
        scales: {
          x: { type: "linear", min: x0, max: Date.now(), grid: { display: false }, border: { color: css("--grid") },
            ticks: { color: css("--text-3"), font, maxTicksLimit: 6, maxRotation: 0, callback: (v) => dTime.format(v) } },
          y: { min: 0, max: 3, position: "right", border: { display: false }, grid: { color: css("--grid"), drawTicks: false },
            ticks: { color: css("--text-3"), font, padding: 8, stepSize: 1, callback: (v) => ({ 0: "0", 1: "1 Med", 2: "2 High", 3: "3" }[v]) } },
        },
      },
    });
    canvas.setAttribute("aria-label", `Stress today, 0 to 3, every 5 minutes. ${n.level ? `Now ${n.level}, ${n1.format(n.score)}.` : n.why}`);
  }

  function render() {
    if (!DATA) return;
    renderAlerts(DATA.alerts || []);
    renderStress(DATA.stress);
  }
  addEventListener("vitals:data", (e) => { DATA = e.detail; render(); });
  addEventListener("vitals:period", () => render());
  addEventListener("vitals:theme", () => render());
})();
