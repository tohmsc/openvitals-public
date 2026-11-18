// Vitals cloud: your WHOOP dashboard on Cloudflare Workers, reading the D1 database
// that cloud_sync.py fills from your Mac. Same front end and JSON shape as dashboard.py.
// Everything except the icons and manifest sits behind a password (DASH_PASSWORD) and a
// signed, HttpOnly session cookie (SESSION_SECRET).

const COOKIE = "vitals_session";
const SESSION_DAYS = 90;
const HISTORY = 400;
const PUBLIC = new Set(["/static/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png", "/static/icon-180.png", "/static/favicon.png", "/favicon.ico"]);
const enc = new TextEncoder();

async function sign(secret, msg) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
function readCookie(req, name) {
  const m = (req.headers.get("cookie") || "").match(new RegExp(`(?:^|; )${name}=([^;]+)`));
  return m ? decodeURIComponent(m[1]) : null;
}
async function authed(req, env) {
  const v = readCookie(req, COOKIE);
  if (!v || !env.SESSION_SECRET) return false;
  const [exp, sig] = v.split(".");
  if (!exp || !sig || Number(exp) < Date.now() / 1000) return false;
  return safeEqual(sig, await sign(env.SESSION_SECRET, exp));
}
const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

const LOGIN = (error) => `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#060708"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Vitals">
<link rel="manifest" href="/static/manifest.webmanifest"><link rel="apple-touch-icon" href="/static/icon-180.png">
<title>Vitals · Sign in</title>
<style>
:root{color-scheme:dark;--bg:#060708;--s:#0f1113;--b:rgb(255 255 255/.09);--t:#f2f4f6;--t2:#a2abb6;--a:#3df5b0;--low:#f45b5f}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#f3f4f6;--s:#fff;--b:rgb(16 24 40/.1);--t:#0b0f14;--t2:#4a5361;--a:#0b9a7f}}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px;background:radial-gradient(900px 420px at 50% -120px,rgb(61 245 176/.08),transparent 70%),var(--bg);color:var(--t);font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,sans-serif;-webkit-font-smoothing:antialiased}
form{width:100%;max-width:360px;padding:28px;border-radius:22px;background:var(--s);box-shadow:0 0 0 1px var(--b),0 24px 60px -30px rgb(0 0 0/.6)}
.mark{width:44px;height:44px;margin-bottom:18px}.mark rect{fill:var(--t)}.mark path{fill:none;stroke:var(--bg);stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}
h1{margin:0 0 4px;font-size:22px;letter-spacing:-.02em}p{margin:0 0 22px;color:var(--t2);font-size:14px}
label{display:block;font-size:13px;color:var(--t2);margin-bottom:6px}
input{width:100%;height:48px;padding:0 14px;border-radius:12px;border:0;font:inherit;font-size:16px;color:var(--t);background:var(--bg);box-shadow:inset 0 0 0 1px var(--b)}
input:focus{outline:2px solid var(--a);outline-offset:1px}
button{width:100%;height:48px;margin-top:14px;border:0;border-radius:12px;font:600 15px/1 inherit;font-family:inherit;color:var(--bg);background:var(--t);cursor:pointer}
.err{margin-top:12px;color:var(--low);font-size:13px}.fine{margin:18px 0 0;font-size:12px;text-align:center}
</style></head><body>
<form method="post" action="/login">
<svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9"/><path d="M5 17h5l3-7 4 13 3-9 2 3h5"/></svg>
<h1>Vitals</h1><p>Your heart, recovery and sleep, synced from your strap.</p>
<label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button>
${error ? `<p class="err" role="alert">${error}</p>` : ""}
<p class="fine">Private dashboard. Not a medical device.</p>
</form></body></html>`;

async function summary(env, url) {
  const since = Number(url.searchParams.get("since")) || Date.now() / 1000 - 86400;
  const DB = env.DB;
  const [profile, recovery, sleep, cycle, workout, soc, logs, meta, today, arch] = await DB.batch([
    DB.prepare("SELECT value FROM api_profile WHERE key='first_name'"),
    DB.prepare("SELECT created_at,recovery_score,hrv_rmssd_ms,resting_hr,spo2,skin_temp_c FROM api_recovery WHERE recovery_score IS NOT NULL ORDER BY created_at DESC LIMIT ?").bind(HISTORY),
    DB.prepare("SELECT start,end,sleep_perf,sleep_efficiency,sleep_consistency,resp_rate,total_in_bed_ms,total_rem_ms,total_sws_ms,total_light_ms,total_awake_ms,disturbances FROM api_sleep WHERE COALESCE(nap,0)=0 AND total_in_bed_ms IS NOT NULL ORDER BY start DESC LIMIT ?").bind(HISTORY),
    DB.prepare("SELECT start,end,strain,avg_hr,max_hr,kilojoule FROM api_cycle WHERE strain IS NOT NULL ORDER BY start DESC LIMIT ?").bind(HISTORY),
    DB.prepare("SELECT start,end,sport_name,strain,avg_hr,max_hr,kilojoule,distance_m,zone_raw FROM api_workout ORDER BY start DESC LIMIT ?").bind(HISTORY),
    DB.prepare("SELECT rtc,soc_pct FROM device_soc ORDER BY rtc"),
    DB.prepare("SELECT rtc,subsystem,message FROM device_logs ORDER BY rtc DESC LIMIT 60"),
    DB.prepare("SELECT key,value FROM meta"),
    DB.prepare("SELECT ts,hr,rr_ms FROM ble_hr WHERE ts >= ? AND hr > 0 ORDER BY ts LIMIT 50000").bind(since),
    DB.prepare("SELECT MIN(created_at) a, MAX(created_at) b FROM api_recovery"),
  ]);
  const m = Object.fromEntries(meta.results.map((r) => [r.key, r.value]));
  let counts = {};
  try { counts = JSON.parse(m.local_counts || "{}"); } catch { /* ignore */ }
  return {
    cloud: true,
    demo: false,
    profile: { first_name: profile.results[0]?.value ?? null },
    recovery: recovery.results,
    sleep: sleep.results,
    cycle: cycle.results,
    workout: workout.results.map(({ zone_raw, ...w }) => {
      let zones = null;
      try { zones = zone_raw ? JSON.parse(zone_raw) : null; } catch { /* ignore */ }
      return { ...w, zones: zones && Object.keys(zones).length ? zones : null };
    }),
    soc: soc.results,
    logs: logs.results,
    today_hr: today.results,
    archive: { start: arch.results[0]?.a ?? null, end: arch.results[0]?.b ?? null },
    sync: { ble: m.last_ble_sync ? Number(m.last_ble_sync) : null, api: m.api_since ?? null, cloud: m.last_cloud_sync ? Number(m.last_cloud_sync) : null },
    counts,
  };
}

async function live(env) {
  const [meta, recent] = await env.DB.batch([
    env.DB.prepare("SELECT value FROM meta WHERE key='live_hr_now'"),
    env.DB.prepare("SELECT ts,hr FROM ble_hr ORDER BY ts DESC LIMIT 120"),
  ]);
  let cur = {};
  try { cur = JSON.parse(meta.results[0]?.value || "{}"); } catch { /* ignore */ }
  const now = Date.now() / 1000;
  const fresh = cur.hr != null && now - (cur.ts || 0) < 8;
  return { live: fresh, hr: cur.hr ?? null, rr_ms: cur.rr_ms || [], ts: cur.ts ?? null, age: cur.ts ? Math.round((now - cur.ts) * 10) / 10 : null, recent: recent.results.reverse() };
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (PUBLIC.has(path)) return env.ASSETS.fetch(req);

    if (path === "/login") {
      if (req.method === "POST") {
        const form = await req.formData();
        const pw = String(form.get("password") || "");
        if (env.DASH_PASSWORD && safeEqual(await sign("pw", pw), await sign("pw", env.DASH_PASSWORD))) {
          const exp = String(Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400);
          const value = `${exp}.${await sign(env.SESSION_SECRET, exp)}`;
          return new Response(null, { status: 303, headers: {
            location: "/",
            "set-cookie": `${COOKIE}=${encodeURIComponent(value)}; Path=/; Max-Age=${SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`,
          } });
        }
        await new Promise((r) => setTimeout(r, 600)); // slow down guessing
        return new Response(LOGIN("That password didn't match."), { status: 401, headers: { "content-type": "text/html; charset=utf-8" } });
      }
      return new Response(LOGIN(""), { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    if (path === "/logout") {
      return new Response(null, { status: 303, headers: { location: "/login", "set-cookie": `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax` } });
    }

    if (!(await authed(req, env))) {
      if (path.startsWith("/api/")) return json({ error: "sign in" }, 401);
      return Response.redirect(new URL("/login", url).toString(), 303);
    }

    if (path === "/api/summary") return json(await summary(env, url));
    if (path === "/api/live") return json(await live(env));
    if (path === "/api/sync") {
      if (req.method === "POST") return json({ error: "The phone can't reach your strap. Your Mac syncs every few minutes; open the dashboard on the Mac to resync now." }, 409);
      return json({ running: false, ok: null, log: [], cloud: true });
    }
    if (path === "/" || path === "/index.html") {
      const page = await env.ASSETS.fetch(new Request(new URL("/", url), { headers: req.headers }));
      return new Response(page.body, { status: page.status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    }
    return env.ASSETS.fetch(req);
  },
};
