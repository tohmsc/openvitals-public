# Optional: off-site backup to Cloudflare D1

Skip this unless the user wants a copy off their machine. Local encrypted backups
(`backup.py`) are the default and are enough for most people.

`cloud_sync.py` pushes every table to a Cloudflare D1 database over the REST API. It runs
at the end of each collector cycle once configured. Note that D1 stores the rows
unencrypted inside the user's own Cloudflare account; tell them that before turning it on.

## Set up
1. Cloudflare dashboard > Storage & Databases > D1 > Create database, e.g. `whoop-backup`.
   Copy its database ID.
2. My Profile > API Tokens > Create token > Custom: permission **Account > D1 > Edit**,
   limited to the account that owns the database.
3. Add to `.env` (never print or commit these):
   ```
   CF_API_TOKEN=...
   CF_D1_DATABASE_ID=...
   # CF_ACCOUNT_ID=...   optional; discovered from the token if omitted
   ```
4. Run once by hand:
   ```bash
   .venv/bin/python cloud_sync.py
   ```
   It prints `cloud sync complete: N rows -> D1 <id>`.

## Turn it off
Remove `CF_API_TOKEN` from `.env`. Delete the D1 database in the dashboard if the user
wants the off-site copy gone.
