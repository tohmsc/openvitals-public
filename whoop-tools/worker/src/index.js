// Vitals cloud: your WHOOP dashboard on Cloudflare Workers, reading the D1 database
// that cloud_sync.py fills from your Mac. Same front end and JSON shape as dashboard.py.
// Everything except the icons, logo, fonts and manifest sits behind a signed, HttpOnly
// session cookie (SESSION_SECRET). You get one with the password (DASH_PASSWORD) or with a
// passkey you added after a password sign-in.

const COOKIE = "vitals_session";
const SESSION_DAYS = 90;
const HISTORY = 400;
const PUBLIC = new Set([
  "/static/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png", "/static/icon-180.png", "/static/favicon.png", "/favicon.ico",
  "/static/smiley.png", "/static/inter-400.woff2", "/static/inter-500.woff2", // the sign-in page uses these
]);
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

const html = (body, status = 200) =>
  new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });

async function startSession(env, location = "/") {
  const exp = String(Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400);
  const value = `${exp}.${await sign(env.SESSION_SECRET, exp)}`;
  return new Response(null, { status: 303, headers: {
    location,
    "set-cookie": `${COOKIE}=${encodeURIComponent(value)}; Path=/; Max-Age=${SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`,
  } });
}

// ───────── Pages: sign in and "add a passkey", in the app's own design ─────────
// Same system as static/app.css: Inter, Radix gray, hairlines instead of shadows,
// the smiley mark, dark by default, light when the app's theme toggle says so.

const SHELL = (title, body, script = "") => `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#fcfcfc" media="(prefers-color-scheme: light)">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Vitals">
<meta name="color-scheme" content="dark light">
<link rel="manifest" href="/static/manifest.webmanifest"><link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="preload" href="/static/inter-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/static/inter-500.woff2" as="font" type="font/woff2" crossorigin>
<title>${title}</title>
<script>try{var t=localStorage.getItem("vitals-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
@font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/inter-400.woff2") format("woff2")}
@font-face{font-family:"Inter";font-weight:500;font-display:swap;src:url("/static/inter-500.woff2") format("woff2")}
:root{color-scheme:dark;--bg:#111111;--surface:#111111;--s2:#191919;--s3:#222222;--border:#2a2a2a;--border-strong:#3a3a3a;--text:#eeeeee;--text-2:#b4b4b4;--text-3:#858585;--accent:oklch(70% 0.16 274deg);--low:#ff6369;--ease-out:cubic-bezier(0.23,1,0.32,1)}
:root[data-theme="light"]{color-scheme:light;--bg:#fcfcfc;--surface:#ffffff;--s2:#f9f9f9;--s3:#f0f0f0;--border:#e8e8e8;--border-strong:#d9d9d9;--text:#202020;--text-2:#646464;--text-3:#737373;--accent:#251cff;--low:#ce2c31}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px;padding:max(24px,env(safe-area-inset-top)) 20px max(24px,env(safe-area-inset-bottom));background:var(--bg);color:var(--text);font:400 0.875rem/1.5 "Inter",ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;letter-spacing:-0.006em;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;font-feature-settings:"cv11","ss01"}
h1,p{margin:0}
.card{width:100%;max-width:360px;padding:32px 24px 24px;border:1px solid var(--border);border-radius:12px;background:var(--surface)}
.brand{display:flex;flex-direction:column;align-items:flex-start;gap:16px;margin-bottom:24px}
.brand img{width:40px;height:40px;display:block}
h1{font-size:1.25rem;font-weight:500;letter-spacing:-0.012em}
.lede{margin-top:4px;color:var(--text-2)}
label{display:block;margin-bottom:6px;font-size:0.8125rem;font-weight:500;color:var(--text-2)}
input{width:100%;height:44px;padding:0 12px;border:1px solid var(--border-strong);border-radius:8px;background:var(--s2);color:var(--text);font:inherit;font-size:1rem;letter-spacing:inherit}
input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;height:44px;padding:0 16px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);color:var(--text);font:inherit;font-weight:500;letter-spacing:inherit;text-decoration:none;cursor:pointer;transition:background-color 150ms,border-color 150ms,transform 120ms var(--ease-out)}
.btn:hover{background:var(--s2)}
.btn:not(:disabled):active{transform:scale(0.97)}
.btn:disabled{opacity:.6;cursor:default}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.btn--primary{background:var(--text);border-color:var(--text);color:var(--bg)}
.btn--primary:hover{background:var(--text);opacity:.9}
.btn--ghost{border-color:transparent;background:transparent;color:var(--text-2)}
.btn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.btn + .btn,input + .btn{margin-top:12px}
.or{display:flex;align-items:center;gap:12px;margin:20px 0;color:var(--text-3);font-size:0.75rem}
.or::before,.or::after{content:"";flex:1;height:1px;background:var(--border)}
.err{margin-top:12px;color:var(--low);font-size:0.8125rem}
.err:empty{display:none}
.fine{color:var(--text-3);font-size:0.75rem;text-align:center}
[hidden]{display:none!important}
@media (prefers-reduced-motion:reduce){.btn{transition:none}.btn:not(:disabled):active{transform:none}}
</style></head><body>
${body}
<p class="fine">Private dashboard. Not a medical device.</p>
${script ? `<script>${CLIENT_HELPERS}${script}</script>` : ""}
</body></html>`;

const KEY_ICON = `<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="6.5" cy="10" r="3.5"/><path d="M10 10h7.5M14.5 10v2.5M17 10v2"/></svg>`;
const BRAND = (title, lede) => `<div class="brand"><img src="/static/smiley.png" alt="" width="40" height="40"><div><h1>${title}</h1><p class="lede">${lede}</p></div></div>`;

// Runs in the browser: base64url <-> bytes, and JSON POST.
const CLIENT_HELPERS = `
var b2u=function(b){var s="",a=new Uint8Array(b);for(var i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);return btoa(s).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"")};
var u2b=function(u){var s=atob(u.replace(/-/g,"+").replace(/_/g,"/")+"===".slice((u.length+3)%4));var a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a.buffer};
var post=function(p,b){return fetch(p,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(b||{}),credentials:"same-origin"}).then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||"Something went wrong.");return j})})};
var canPasskey=!!(window.PublicKeyCredential&&navigator.credentials);
var mark=function(){try{localStorage.setItem("vitals-passkey","1")}catch(e){}};
`;

const LOGIN = (error, hasPasskeys) => SHELL("Vitals · Sign in", `<main class="card">
${BRAND("Vitals", "Your heart, recovery and sleep, synced from your strap.")}
<div id="pk" ${hasPasskeys ? "" : "hidden"}>
<button class="btn btn--primary" type="button" id="pk-btn">${KEY_ICON}Sign in with a passkey</button>
<div class="or">or use your password</div>
</div>
<form method="post" action="/login">
<label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required ${hasPasskeys ? "" : "autofocus"}>
<button class="btn ${hasPasskeys ? "" : "btn--primary"}" type="submit">Sign in</button>
</form>
<p class="err" id="err" role="alert">${error}</p>
</main>`, `
var box=document.getElementById("pk"),btn=document.getElementById("pk-btn"),err=document.getElementById("err");
if(!canPasskey)box.hidden=true;
btn.addEventListener("click",function(){
  err.textContent="";btn.disabled=true;
  post("/passkey/login/options").then(function(o){
    return navigator.credentials.get({publicKey:{challenge:u2b(o.challenge),rpId:o.rpId,userVerification:"required",timeout:120000,allowCredentials:[]}});
  }).then(function(c){
    var r=c.response;
    return post("/passkey/login",{id:c.id,clientDataJSON:b2u(r.clientDataJSON),authenticatorData:b2u(r.authenticatorData),signature:b2u(r.signature)});
  }).then(function(){mark();location.replace("/")}).catch(function(e){
    btn.disabled=false;
    if(e&&e.name==="NotAllowedError")return; // cancelled
    err.textContent=e&&e.message?e.message:"That passkey didn't work.";
  });
});`);

const PASSKEY_OFFER = () => SHELL("Vitals · Add a passkey", `<main class="card">
${BRAND("Sign in faster next time", "Add a passkey and unlock Vitals with Face ID, Touch ID or your device code. No password to type.")}
<button class="btn btn--primary" type="button" id="add">${KEY_ICON}Add a passkey</button>
<a class="btn btn--ghost" href="/" id="skip">Not now</a>
<p class="err" id="err" role="alert"></p>
</main>`, `
var add=document.getElementById("add"),err=document.getElementById("err");
var seen=false;try{seen=localStorage.getItem("vitals-passkey")==="1"}catch(e){}
if(!canPasskey||(seen&&!/[?&]again\\b/.test(location.search)))location.replace("/");
add.addEventListener("click",function(){
  err.textContent="";add.disabled=true;
  post("/passkey/register/options").then(function(o){
    o.challenge=u2b(o.challenge);o.user.id=u2b(o.user.id);
    o.excludeCredentials=o.excludeCredentials.map(function(c){return {type:"public-key",id:u2b(c.id)}});
    return navigator.credentials.create({publicKey:o});
  }).then(function(c){
    var r=c.response,pk=r.getPublicKey&&r.getPublicKey();
    if(!pk)throw new Error("This browser can't save a passkey here. Use the password instead.");
    return post("/passkey/register",{id:c.id,clientDataJSON:b2u(r.clientDataJSON),authenticatorData:b2u(r.getAuthenticatorData()),publicKey:b2u(pk),alg:r.getPublicKeyAlgorithm(),transports:r.getTransports?r.getTransports():[]});
  }).then(function(){mark();location.replace("/")}).catch(function(e){
    add.disabled=false;
    if(e&&e.name==="InvalidStateError"){mark();location.replace("/");return} // this device already has one
    if(e&&e.name==="NotAllowedError")return;
    err.textContent=e&&e.message?e.message:"Couldn't add the passkey.";
  });
});`);

// ───────── Passkeys (WebAuthn) ─────────
// Discoverable credentials with user verification, so signing in needs no username.
// Public keys live in D1 (table passkeys; cloud_sync.py never touches it). Challenges
// are signed with SESSION_SECRET, expire in 5 minutes and are used once.

const b64u = {
  enc: (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
  dec: (s) => Uint8Array.from(atob(String(s).replace(/-/g, "+").replace(/_/g, "/") + "===".slice((String(s).length + 3) % 4)), (c) => c.charCodeAt(0)),
};
const sha256 = async (data) => new Uint8Array(await crypto.subtle.digest("SHA-256", typeof data === "string" ? enc.encode(data) : data));
const ALGS = {
  "-7": { imp: { name: "ECDSA", namedCurve: "P-256" }, ver: { name: "ECDSA", hash: "SHA-256" }, der: true },
  "-8": { imp: { name: "Ed25519" }, ver: { name: "Ed25519" } },
  "-257": { imp: { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, ver: { name: "RSASSA-PKCS1-v1_5" } },
};

let schemaReady = false;
async function ensureSchema(env) {
  if (schemaReady) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS passkeys (id TEXT PRIMARY KEY, public_key TEXT NOT NULL, alg INTEGER NOT NULL, sign_count INTEGER NOT NULL DEFAULT 0, transports TEXT, device TEXT, created_at REAL, last_used REAL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS passkey_challenges (nonce TEXT PRIMARY KEY, exp REAL NOT NULL)"),
    // strap score estimates, pushed by cloud_sync.py (here too so a summary before the first push works)
    env.DB.prepare("CREATE TABLE IF NOT EXISTS estimates (date TEXT, kind TEXT, version TEXT, value REAL, ok INTEGER, why TEXT, row TEXT, inputs TEXT, computed_at REAL, PRIMARY KEY (date, kind, version))"),
    // stress monitor windows, pushed by cloud_sync.py (stress.py on the Mac)
    env.DB.prepare("CREATE TABLE IF NOT EXISTS stress (ts REAL PRIMARY KEY, date TEXT, state TEXT, score REAL, hr REAL, rmssd REAL, n_rr INTEGER, move REAL, computed_at REAL)"),
  ]);
  schemaReady = true;
}
async function passkeyCount(env) {
  try { await ensureSchema(env); return (await env.DB.prepare("SELECT COUNT(*) n FROM passkeys").first("n")) || 0; } catch { return 0; }
}

async function newChallenge(env, purpose) {
  const exp = Math.floor(Date.now() / 1000) + 300;
  const nonce = b64u.enc(crypto.getRandomValues(new Uint8Array(16)));
  const body = `${purpose}.${exp}.${nonce}`;
  return b64u.enc(enc.encode(`${body}.${await sign(env.SESSION_SECRET, body)}`));
}
async function useChallenge(env, purpose, challenge) {
  const parts = new TextDecoder().decode(b64u.dec(challenge)).split(".");
  if (parts.length !== 4) return false;
  const [p, exp, nonce, sig] = parts;
  if (p !== purpose || Number(exp) < Date.now() / 1000) return false;
  if (!safeEqual(sig, await sign(env.SESSION_SECRET, `${p}.${exp}.${nonce}`))) return false;
  await env.DB.prepare("DELETE FROM passkey_challenges WHERE exp < ?").bind(Date.now() / 1000).run();
  const r = await env.DB.prepare("INSERT OR IGNORE INTO passkey_challenges (nonce, exp) VALUES (?, ?)").bind(nonce, Number(exp)).run();
  return r.meta.changes === 1; // a second use of the same challenge fails
}

// Shared checks for clientDataJSON and authenticatorData. Returns the parsed flags and counter.
async function checkCeremony(env, url, type, purpose, clientDataB64, authDataB64) {
  const clientRaw = b64u.dec(clientDataB64);
  let client;
  try { client = JSON.parse(new TextDecoder().decode(clientRaw)); } catch { throw new Error("bad client data"); }
  if (client.type !== type) throw new Error("wrong ceremony");
  if (client.origin !== url.origin) throw new Error("wrong origin");
  if (!(await useChallenge(env, purpose, client.challenge))) throw new Error("expired");
  const auth = b64u.dec(authDataB64);
  if (auth.length < 37) throw new Error("bad authenticator data");
  const rpHash = await sha256(url.hostname);
  if (!safeEqual(b64u.enc(auth.slice(0, 32)), b64u.enc(rpHash))) throw new Error("wrong site");
  const flags = auth[32];
  if (!(flags & 0x01) || !(flags & 0x04)) throw new Error("user not verified"); // UP and UV
  const counter = new DataView(auth.buffer, auth.byteOffset + 33, 4).getUint32(0);
  return { clientRaw, auth, counter };
}

// WebAuthn ECDSA signatures are DER; WebCrypto wants raw r||s.
function derToRaw(der) {
  let i = 2;
  if (der[1] & 0x80) i = 2 + (der[1] & 0x7f);
  const part = () => {
    i++; // 0x02
    const len = der[i++];
    let x = der.slice(i, i + len);
    i += len;
    while (x.length > 32 && x[0] === 0) x = x.slice(1);
    const out = new Uint8Array(32);
    out.set(x, 32 - x.length);
    return out;
  };
  const r = part(), s = part();
  const raw = new Uint8Array(64);
  raw.set(r); raw.set(s, 32);
  return raw;
}

async function registerOptions(env, url) {
  await ensureSchema(env);
  const [name, existing] = await env.DB.batch([
    env.DB.prepare("SELECT value FROM api_profile WHERE key='first_name'"),
    env.DB.prepare("SELECT id FROM passkeys"),
  ]);
  const who = name.results[0]?.value || "Vitals";
  return {
    challenge: await newChallenge(env, "reg"),
    rp: { name: "Vitals", id: url.hostname },
    user: { id: b64u.enc(enc.encode("vitals-owner")), name: who, displayName: who },
    pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -8 }, { type: "public-key", alg: -257 }],
    authenticatorSelection: { residentKey: "required", requireResidentKey: true, userVerification: "required" },
    excludeCredentials: existing.results.map((r) => ({ type: "public-key", id: r.id })),
    attestation: "none",
    timeout: 120000,
  };
}

async function register(env, url, req, body) {
  const alg = ALGS[String(body.alg)];
  if (!alg) throw new Error("This passkey type isn't supported.");
  if (typeof body.id !== "string" || !body.id || body.id.length > 1024) throw new Error("bad id");
  await checkCeremony(env, url, "webauthn.create", "reg", body.clientDataJSON, body.authenticatorData);
  await crypto.subtle.importKey("spki", b64u.dec(body.publicKey), alg.imp, false, ["verify"]); // must parse
  const ua = (req.headers.get("user-agent") || "").slice(0, 200);
  await env.DB.prepare("INSERT OR REPLACE INTO passkeys (id, public_key, alg, sign_count, transports, device, created_at, last_used) VALUES (?, ?, ?, 0, ?, ?, ?, NULL)")
    .bind(body.id, body.publicKey, Number(body.alg), JSON.stringify(body.transports || []), ua, Date.now() / 1000).run();
}

async function verifyLogin(env, url, body) {
  await ensureSchema(env);
  const row = typeof body.id === "string" ? await env.DB.prepare("SELECT * FROM passkeys WHERE id = ?").bind(body.id).first() : null;
  if (!row) throw new Error("unknown passkey");
  const alg = ALGS[String(row.alg)];
  const { clientRaw, auth, counter } = await checkCeremony(env, url, "webauthn.get", "auth", body.clientDataJSON, body.authenticatorData);
  const signed = new Uint8Array(auth.length + 32);
  signed.set(auth); signed.set(await sha256(clientRaw), auth.length);
  let sig = b64u.dec(body.signature);
  if (alg.der) sig = derToRaw(sig);
  const key = await crypto.subtle.importKey("spki", b64u.dec(row.public_key), alg.imp, false, ["verify"]);
  if (!(await crypto.subtle.verify(alg.ver, key, sig, signed))) throw new Error("bad signature");
  if (counter !== 0 || row.sign_count !== 0) {
    if (counter <= row.sign_count) throw new Error("cloned passkey");
  }
  await env.DB.prepare("UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?").bind(counter, Date.now() / 1000, row.id).run();
}

async function summary(env, url) {
  const since = Number(url.searchParams.get("since")) || Date.now() / 1000 - 86400;
  const DB = env.DB;
  await ensureSchema(env);
  const [profile, recovery, sleep, cycle, workout, soc, logs, meta, today, arch, est] = await DB.batch([
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
    // only the estimates that passed their checks, of the version the Mac says is current
    DB.prepare("SELECT date, kind, version, ok, why, row, inputs FROM estimates WHERE version = (SELECT value FROM meta WHERE key = 'scores_version') AND (ok = 1 OR date >= date('now', '-14 days')) ORDER BY date DESC LIMIT ?").bind(HISTORY),
  ]);
  const m = Object.fromEntries(meta.results.map((r) => [r.key, r.value]));
  // how you slept (sleep.py on the Mac); the table appears with the first push that has a night
  let nights = [];
  try {
    nights = (await DB.prepare("SELECT date,onset,wake,asleep_s,awake_s,wakeups,rhr,hrv,usual_s,status,headline,detail,segments FROM sleep_night ORDER BY date DESC LIMIT 30").all()).results;
  } catch { /* not pushed yet */ }
  let counts = {};
  try { counts = JSON.parse(m.local_counts || "{}"); } catch { /* ignore */ }
  // the strap's own answer (daemon.py), not the newest firmware log line: a history drain
  // replays old logs, so those can be months old
  const reading = (v) => {
    try {
      const b = JSON.parse(v || "null");
      return b && b.src === "strap" && Date.now() / 1000 - b.ts < 86400 ? b.soc : null;
    } catch { return null; }
  };
  const battery = reading(m.battery_now);
  const pack_battery = reading(m.pack_battery_now);   // Battery Level characteristic: likely the pack
  // stress monitor: the newest day the Mac pushed (the app checks it is today)
  let stress = null;
  try {
    const rows = (await DB.prepare("SELECT ts, date, state, score, hr, rmssd FROM stress WHERE date = (SELECT MAX(date) FROM stress) ORDER BY ts").all()).results;
    if (rows.length) stress = stressFor(rows, JSON.parse(m.stress_baseline || "null"), Date.now() / 1000);
  } catch { /* not pushed yet */ }
  let alerts = [];
  try { alerts = JSON.parse(m.health_alerts || "[]"); } catch { /* ignore */ }
  let coverage = [];
  try { coverage = JSON.parse(m.coverage || "[]"); } catch { /* ignore */ }   // metrics.coverage_days on the Mac
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
    battery,
    pack_battery,
    logs: logs.results,
    today_hr: today.results,
    archive: { start: arch.results[0]?.a ?? null, end: arch.results[0]?.b ?? null },
    nights,
    estimates: estimates(est.results),
    stress,
    alerts,
    sync: { ble: m.last_ble_sync ? Number(m.last_ble_sync) : null, api: m.api_since ?? null, cloud: m.last_cloud_sync ? Number(m.last_cloud_sync) : null },
    counts,
    coverage,
  };
}

// Same shape as scores.for_app() on the Mac: rows like the WHOOP tables, marked with `est`.
function estimates(rows) {
  const out = { recovery: [], sleep: [], cycle: [], held: [] };
  const to = { recovery: "recovery", sleep: "sleep", strain: "cycle" };
  for (const r of rows) {
    if (!r.ok) { out.held.push({ date: r.date, kind: r.kind, why: r.why }); continue; }
    let row, inputs;
    try { row = JSON.parse(r.row); inputs = JSON.parse(r.inputs || "{}"); } catch { continue; }
    if (to[r.kind]) out[to[r.kind]].push({ ...row, est: r.version, est_date: r.date, basis: inputs.basis ?? null });
  }
  return out;
}

// Same as stress.for_app() / stress.now_value() on the Mac.
const STRESS_WHY = {
  asleep: "Asleep.",
  no_motion: "The strap's movement data for this time hasn't come in yet.",
  moving: "You're moving, so a raised heart rate says nothing about stress.",
  active: "Heart rate is in an exercise zone.",
  settling: "Just stopped moving; heart rate is still settling.",
  few_beats: "Not enough clean beats to read HRV.",
};
const stressLevel = (s) => (s == null ? null : s >= 2 ? "High" : s >= 1 ? "Medium" : "Low");
function stressFor(rows, baseline, t) {
  const series = rows.map((r) => ({ ts: r.ts, state: r.state, score: r.score, hr: r.hr, rmssd: r.rmssd == null ? null : Math.round(r.rmssd * 10) / 10 }));
  const recent = series.filter((w) => w.ts + 300 >= t - 1800 && w.state !== "no_data");
  const told = recent.filter((w) => w.state !== "no_motion");
  let now;
  if (told.length) {
    const w = told.at(-1);
    now = { ts: w.ts, state: w.state, score: w.score, level: stressLevel(w.score), why: w.state === "ok" ? null : STRESS_WHY[w.state], pending: recent.at(-1).state === "no_motion" };
  } else if (recent.length) now = { ts: null, state: "no_motion", score: null, level: null, why: STRESS_WHY.no_motion, pending: true };
  else now = { ts: null, state: "no_data", score: null, level: null, why: "Nothing from the strap in the last 30 minutes.", pending: false };
  return { date: rows[0].date, now, baseline, series };
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
          return startSession(env, "/passkey"); // offer a passkey; the page skips itself if this device has one
        }
        await new Promise((r) => setTimeout(r, 600)); // slow down guessing
        return html(LOGIN("That password didn't match.", (await passkeyCount(env)) > 0), 401);
      }
      if (await authed(req, env)) return Response.redirect(new URL("/", url).toString(), 303);
      return html(LOGIN("", (await passkeyCount(env)) > 0));
    }
    if (path === "/passkey/login/options" && req.method === "POST") {
      await ensureSchema(env);
      return json({ challenge: await newChallenge(env, "auth"), rpId: url.hostname });
    }
    if (path === "/passkey/login" && req.method === "POST") {
      try {
        await verifyLogin(env, url, await req.json());
      } catch {
        await new Promise((r) => setTimeout(r, 600));
        return json({ error: "That passkey didn't work. Try again or use your password." }, 401);
      }
      const res = await startSession(env, "/");
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json", "set-cookie": res.headers.get("set-cookie") } });
    }
    if (path === "/logout") {
      return new Response(null, { status: 303, headers: { location: "/login", "set-cookie": `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax` } });
    }

    if (!(await authed(req, env))) {
      if (path.startsWith("/api/")) return json({ error: "sign in" }, 401);
      return Response.redirect(new URL("/login", url).toString(), 303);
    }

    if (path === "/passkey" && req.method === "GET") return html(PASSKEY_OFFER());
    if (path === "/passkey/register/options" && req.method === "POST") return json(await registerOptions(env, url));
    if (path === "/passkey/register" && req.method === "POST") {
      try {
        await register(env, url, req, await req.json());
      } catch (e) {
        return json({ error: e.message === "This passkey type isn't supported." ? e.message : "Couldn't save the passkey. Try again." }, 400);
      }
      return json({ ok: true });
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
