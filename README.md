# Prestige Fabrications — Enquiry, Quoting & Estimating Control (Phase 2)

Phase 1 was a clickable prototype that reset every refresh. **Phase 2 turns it into real
software you can run and pilot**: real per-user logins, a database that keeps your data,
file storage on disk, and five new workflow features layered on top.

It still looks and works like the prototype your team already saw — it now just *remembers*.

---

## Running it

You need **Python 3.8 or newer**. Nothing else — no Node, no `pip install`, no database
to set up. The server uses only Python's standard library.

```bash
cd prestige
python3 app.py
```

Then open **http://localhost:8000** in a web browser.

To let the rest of the office use it, run it on one always-on PC or a small server and
have everyone browse to `http://THAT-PCS-IP-ADDRESS:8000`. (The browser loads the React UI
from a public CDN on first visit, so that machine needs internet access. The data itself
never leaves your network.)

Stop the server with **Ctrl+C**.

---

## Logins

Five accounts are created automatically. Each person signs in with their own account so the
audit trail stays accurate. **Set a real password for every account before you share the URL**
(see step 4 of the deployment guide) — until you do, each account's password is the same as its
username, which is fine on your own machine but not on a public address. The server prints a
warning at startup listing any account still on a default password.

| Username | Name        | Role        |
|----------|-------------|-------------|
| sarah    | Sarah Chen  | Admin       |
| mark     | Mark Reilly | Estimator   |
| priya    | Priya Patel | Estimator   |
| dave     | Dave Holt   | Sales/Admin |
| janet    | Janet Cole  | Management  |

These names are placeholders — tell the developer your real team and roles to have them set up,
or edit the `SEED_USERS` list near the top of `app.py`. Signing out is in the top-right menu.

---

## What's stored where

- **`prestige.db`** — a SQLite database file, created automatically on first run. All
  enquiries, quotes, notes, chases, supplier prices, approvals and notifications live here.
  Back it up by copying this one file. *(On Railway this sits in the volume — see deployment.)*
- **`uploads/`** — uploaded drawings, CAD files, quotes and POs are saved here. *(Also on the
  volume when hosted.)*
- **`index.html`** — the application interface (served by `app.py`).

The board starts **empty** — a real, clean system. If you'd like it pre-filled with sample
enquiries for a trial or for training, set the variable `LOAD_DEMO_DATA` = `1` and the demo
dataset loads on first login. Leave it unset for live use. Everything you enter is saved.

---

## New in Phase 2

1. **Real logins, real database, real files.** Data persists across restarts; documents are
   uploaded and stored on disk; the SigmaMRP duplicate check and enquiry numbering are now
   enforced by the server, not just the browser.
2. **Management approval workflow.** Quotes at or above £15,000 (or flagged manually) must be
   approved by a Management user before they can be sent. Estimators request sign-off;
   managers approve or reject with a note; both sides are notified; the dashboard shows a
   live "Approvals pending" count.
3. **Supplier price request tracking.** A new *Supplier prices* tab on each enquiry: request a
   price from a supplier with a due date, see it flagged overdue if it's late, and log the
   returned price. Totals feed into your line-item costs.
4. **Automatic chase task creation.** Marking a quote as sent auto-schedules the first chase
   (3 days). Logging a chase automatically books the next one on the 3 / 7 / 14 / 30-day plan
   and assigns it to the chaser — no more relying on memory.
5. **Estimated-vs-actual costs + PDF reports.** On won jobs you can enter actual
   material/labour/subcontract/coating costs and see variance and true margin. The Reports
   page gains an estimated-vs-actual summary and a **Save as PDF** button for management
   reports.

---

## Putting it on the web (GitHub + Railway)

This app is ready to deploy to [Railway](https://railway.app). It needs **no build step and no
dependencies** — but there is **one thing you must set up or you'll lose your data**: a
persistent volume (see step 3). Railway's normal filesystem is wiped on every redeploy.

### 1. Push to GitHub
Create a new repository and add these files (this is the whole app):

```
app.py            index.html        README.md
requirements.txt  Procfile          railway.json
.python-version   .gitignore
```

The `.gitignore` keeps the local database and uploads out of the repo, which is what you want —
the live copies live on Railway.

### 2. Create the Railway project
- In Railway: **New Project → Deploy from GitHub repo**, and pick your repo.
- Railway detects Python automatically and starts it with `python app.py`.
- Under the service's **Settings → Networking**, click **Generate Domain** to get a public URL.

You do **not** need to set a port — Railway provides one and the app reads it automatically.

### 3. Add a persistent volume (important — do this before relying on it)
Without this, `prestige.db` and uploaded files reset every time you redeploy.

- On the service, add a **Volume** and set its mount path to **`/data`**.
- Add a service **Variable**: `DATA_DIR` = `/data`.

Now the database and uploads live on the volume and survive restarts and redeploys.

### 4. Set real passwords (important on a public URL)
The demo passwords (`mark`/`mark` etc.) are public knowledge, so on an internet-facing URL set
your own. Add a service **Variable** for each person you want to keep:

```
PF_PW_SARAH = your-admin-password
PF_PW_MARK  = your-password
PF_PW_PRIYA = your-password
PF_PW_DAVE  = your-password
PF_PW_JANET = your-password
```

These take effect on the next deploy. Usernames stay the same (sarah, mark, priya, dave, janet);
only the passwords change. Any user without a variable keeps the default demo password.

That's it — open your Railway domain and sign in.

---

## Going live — quick checklist

1. **Volume attached at `/data`** and variable `DATA_DIR=/data` set, so data persists.
2. **A password set for every account** via `PF_PW_*` variables (the startup log warns about
   any you've missed).
3. **`LOAD_DEMO_DATA` left unset** so the board is empty and real.
4. **Daily backups turned on** for the volume (Railway → service → Backups), plus the occasional
   off-platform copy of `prestige.db`.

### Starting from a clean board

If you already opened the app once with demo data loaded, those sample enquiries are now saved on
the volume. To wipe them and begin fresh: in Railway, open the **volume** and use **Wipe Volume**
(service Settings), then redeploy. The app re-initialises empty (just the user accounts), and with
`LOAD_DEMO_DATA` unset no sample data comes back.

---

## Security / production notes

This is a working pilot, not a hardened production system. On a public deployment:

- **Set the `PF_PW_*` passwords** so the default (username) logins don't work. The server lists
  any still-default account in its startup log.
- Railway serves over HTTPS by default — good. If you host it elsewhere, put it behind HTTPS.
- Sessions are held in memory, so a redeploy or restart signs everyone out — they just log
  back in. (The data itself is safe on the volume.)
- The accounts are fixed in `app.py`; there's no self-service sign-up or password-reset screen yet.
- Consider vendoring the front-end libraries locally if any machines are offline (the browser
  currently loads React from a public CDN).

## Still future-phase

Not yet included (these need external systems or longer build time): live SigmaMRP API
integration, email-to-enquiry auto-capture and Outlook integration, a website enquiry form
feed, and fully enforced role permissions beyond the approval gate.
