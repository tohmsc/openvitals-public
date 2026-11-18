#!/usr/bin/env python3
"""Move old raw strap frames to Cloudflare R2, then delete them locally.

ble_raw (the lossless copy of everything the strap sent) grows by roughly 20 MB a day.
This ships it off the laptop, one object per complete UTC hour:

  1. pack the hour's rows as gzipped NDJSON, AES-256 encrypt with .backup_key if present
  2. upload to R2 with the Cloudflare CLI (wrangler)
  3. download it again and check it is byte-identical and still unpacks to every row
  4. only then delete those rows from the local database

A failed upload or check deletes nothing; the hour is retried on the next run.
Decoded data (ble_hr, battery, logs) stays local — only raw frames are moved, and only
frames decode.py has already processed and that are older than the keep window.

Setup: wrangler logged in (`npx wrangler login`), then in .env:
  CF_R2_BUCKET=whoop-raw-archive      # turns this on (the daemon then runs it hourly)
  CF_R2_KEEP_HOURS=24                 # optional: how much raw data to keep locally
  CF_R2_API_TOKEN=...                 # optional: use a token (Workers R2 Storage: Edit)
                                      # instead of the wrangler login

Run:  python archive.py                      # archive + prune whatever is due
      python archive.py status               # what has been archived
      python archive.py restore out.db [--from 2026-11-18T10] [--to 2026-11-18T14]
                                             # fetch archived frames back into a separate DB
"""
import gzip, hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, time
import config, db

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.join(HERE, ".backup_key")
COLS = ("rowid", "ts", "channel", "ptype", "hex")


def env():
    return config.env(os.path.join(HERE, ".env"))


def configured():
    return bool(env().get("CF_R2_BUCKET"))


# ---------------- storage ----------------
class R2:
    """R2 through the Cloudflare CLI. Uses the wrangler login unless CF_R2_API_TOKEN is set."""

    def __init__(self, bucket, e=None):
        e = e if e is not None else env()
        self.bucket = bucket
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CF_API_TOKEN", "CLOUDFLARE_API_TOKEN")}   # the D1 token can't do R2
        self.env["PATH"] = os.pathsep.join([self.env.get("PATH", ""), os.path.expanduser("~/.local/bin"),
                                            "/opt/homebrew/bin", "/usr/local/bin"])   # launchd's PATH is bare
        if e.get("CF_R2_API_TOKEN"):
            self.env["CLOUDFLARE_API_TOKEN"] = e["CF_R2_API_TOKEN"]
        if e.get("CF_ACCOUNT_ID"):
            self.env["CLOUDFLARE_ACCOUNT_ID"] = e["CF_ACCOUNT_ID"]
        self.cmd = None

    def _wrangler(self, *args):
        if self.cmd is None:
            path = self.env["PATH"]
            if shutil.which("wrangler", path=path):
                self.cmd = [shutil.which("wrangler", path=path)]
            elif shutil.which("npx", path=path):
                npx = shutil.which("npx", path=path)
                cached = subprocess.run([npx, "--no-install", "wrangler", "--version"], env=self.env,
                                        cwd=tempfile.gettempdir(), capture_output=True, timeout=120)
                self.cmd = [npx, "--no-install" if cached.returncode == 0 else "--yes", "wrangler"]
            else:
                raise RuntimeError("Cloudflare CLI not found: install Node, then `npm i -g wrangler`")
        # run outside this folder: wrangler would otherwise pick up the D1-only token from .env
        r = subprocess.run(self.cmd + list(args), env=self.env, cwd=tempfile.gettempdir(),
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"wrangler {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout

    def put(self, key, path):
        self._wrangler("r2", "object", "put", f"{self.bucket}/{key}", "--file", path, "--remote")

    def get(self, key, path):
        self._wrangler("r2", "object", "get", f"{self.bucket}/{key}", "--file", path, "--remote")


# ---------------- packing ----------------
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _openssl(args):
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", *args, "-pass", f"file:{KEY}"], check=True)


def pack(rows, path):
    """Write rows to path (gzip NDJSON, encrypted if .backup_key exists). Returns (path, encrypted)."""
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in COLS}, separators=(",", ":")) + "\n")
    if not os.path.exists(KEY):
        return path, False
    _openssl(["-salt", "-in", path, "-out", path + ".enc"])
    os.remove(path)
    return path + ".enc", True


def unpack(path, encrypted):
    """Read a packed file back into a list of row dicts."""
    if encrypted:
        plain = path + ".plain"
        _openssl(["-d", "-in", path, "-out", plain])
        path = plain
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def key_for(hour, first, last, encrypted):
    t = time.gmtime(hour * 3600)
    return (f"raw/{t.tm_year:04d}/{t.tm_mon:02d}/{t.tm_mday:02d}/"
            f"ble_raw-{time.strftime('%Y%m%dT%H', t)}Z-{first}-{last}.ndjson.gz" + (".enc" if encrypted else ""))


# ---------------- archive + prune ----------------
def run(store=None, keep_hours=None, quiet=False):
    """Archive every complete hour older than the keep window. Returns the number of hours moved."""
    e = env()
    if store is None:
        if not e.get("CF_R2_BUCKET"):
            print("Set CF_R2_BUCKET in .env first (see the top of archive.py).")
            return 0
        store = R2(e["CF_R2_BUCKET"], e)
    if keep_hours is None:
        keep_hours = float(e.get("CF_R2_KEEP_HOURS") or 24)
    c = db.connect()
    decoded = int(db.get_state(c, "decode_rowid", 0) or 0)
    newest = c.execute("SELECT MAX(rowid) m FROM ble_raw").fetchone()["m"] or 0
    before = int((time.time() - keep_hours * 3600) // 3600)          # first hour that must stay
    hours = [r["h"] for r in c.execute(
        "SELECT DISTINCT CAST(ts / 3600 AS INTEGER) h FROM ble_raw WHERE ts < ? ORDER BY h", (before * 3600,))]
    moved = 0
    for hour in hours:
        rows = c.execute(f"SELECT {','.join(COLS)} FROM ble_raw WHERE ts >= ? AND ts < ? ORDER BY rowid",
                         (hour * 3600, (hour + 1) * 3600)).fetchall()
        first, last = rows[0]["rowid"], rows[-1]["rowid"]
        # Never move frames decode.py hasn't read yet, and never the newest row: SQLite hands
        # out rowid = max + 1, so emptying the table would restart numbering below decode's
        # watermark and new frames would be skipped. (For the same reason: don't VACUUM.)
        if last > decoded or last >= newest:
            break
        with tempfile.TemporaryDirectory(prefix="whoop-archive-") as tmp:
            path, enc = pack(rows, os.path.join(tmp, "up.ndjson.gz"))
            key, digest, size = key_for(hour, first, last, enc), sha256(path), os.path.getsize(path)
            try:
                store.put(key, path)
                back = os.path.join(tmp, "down")
                store.get(key, back)
                if sha256(back) != digest:
                    raise RuntimeError("downloaded copy differs from what was uploaded")
                if [r["rowid"] for r in unpack(back, enc)] != [r["rowid"] for r in rows]:
                    raise RuntimeError("downloaded copy does not unpack to the same rows")
            except Exception as ex:
                print(f"archive: {key} not confirmed, keeping the local rows ({ex})")
                break
        c.execute("INSERT OR REPLACE INTO raw_archive(key,hour,first_rowid,last_rowid,rows,bytes,sha256,encrypted,archived_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (key, hour, first, last, len(rows), size, digest, int(enc), time.time()))
        c.execute("DELETE FROM ble_raw WHERE rowid >= ? AND rowid <= ? AND ts >= ? AND ts < ?",
                  (first, last, hour * 3600, (hour + 1) * 3600))
        c.commit()                                    # index row and delete land together
        moved += 1
        if not quiet:
            print(f"archived {len(rows)} raw frames -> {key} ({size // 1024} KB), removed locally")
    if moved and quiet:
        print(f"archive: moved {moved} hour(s) of raw frames to R2 and removed them locally")
    return moved


def status():
    c = db.connect()
    r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(rows),0) rows, COALESCE(SUM(bytes),0) b, "
                  "MIN(hour) a, MAX(hour) z FROM raw_archive").fetchone()
    local = c.execute("SELECT COUNT(*) n, MIN(ts) a FROM ble_raw").fetchone()
    hr = lambda h: time.strftime("%Y-%m-%d %H:00Z", time.gmtime(h * 3600))
    if r["n"]:
        print(f"in R2: {r['n']} objects, {r['rows']} raw frames, {r['b'] / 1e6:.1f} MB, {hr(r['a'])} to {hr(r['z'])}")
    else:
        print("in R2: nothing archived yet")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(local["a"])) if local["a"] else "-"
    print(f"local: {local['n']} raw frames, oldest from {since}")


def _hour(s):
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            import calendar
            return calendar.timegm(time.strptime(s, fmt)) // 3600
        except ValueError:
            pass
    raise SystemExit(f"can't read time {s!r}: use 2026-11-18 or 2026-11-18T10 (UTC)")


def restore(out_path, start=None, end=None, store=None):
    """Download archived frames into a separate SQLite file (never into the live DB)."""
    e = env()
    store = store or R2(e["CF_R2_BUCKET"], e)
    c = db.connect()
    q, a = "SELECT * FROM raw_archive WHERE 1=1", []
    if start is not None: q += " AND hour >= ?"; a.append(start)
    if end is not None: q += " AND hour < ?"; a.append(end)
    out = sqlite3.connect(out_path)
    out.execute("CREATE TABLE IF NOT EXISTS ble_raw (ts REAL, channel TEXT, ptype INTEGER, hex TEXT)")
    total = 0
    for a_row in c.execute(q + " ORDER BY hour", a).fetchall():
        with tempfile.TemporaryDirectory(prefix="whoop-restore-") as tmp:
            path = os.path.join(tmp, "down")
            store.get(a_row["key"], path)
            if sha256(path) != a_row["sha256"]:
                raise SystemExit(f"{a_row['key']}: checksum mismatch, stopping")
            rows = unpack(path, a_row["encrypted"])
        out.executemany("INSERT OR REPLACE INTO ble_raw(rowid,ts,channel,ptype,hex) VALUES(?,?,?,?,?)",
                        [tuple(r[k] for k in COLS) for r in rows])
        out.commit()
        total += len(rows)
        print(f"restored {len(rows)} frames from {a_row['key']}")
    print(f"{total} raw frames -> {out_path}")
    return total


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["status"]:
        status()
    elif args[:1] == ["restore"] and len(args) > 1:
        opt = lambda name: _hour(args[args.index(name) + 1]) if name in args else None
        restore(args[1], opt("--from"), opt("--to"))
    else:
        keep = float(args[args.index("--keep-hours") + 1]) if "--keep-hours" in args else None
        run(keep_hours=keep)
        status()
