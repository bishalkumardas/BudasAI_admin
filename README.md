# BudasAI Streamlit Admin

## 1. Prepare Supabase

Run [`setup.sql`](setup.sql) in the project SQL Editor. The market-index form writes the full display name to `markets.name` and the Yahoo Finance symbol to `markets.symbol`; it imports the latest closing price into `markets.latest_price`. The `market_history` import assumes these columns:

`market_id`, `timestamp`, `open`, `high`, `low`, `close`, `volume`

The unique index is needed for upserts. If it reports duplicate historical rows, merge or remove those duplicate `(market_id, timestamp)` pairs once, then rerun the statement.

It also assumes `research_articles` has an `id` primary key plus `title`; optional fields used by the focused article form are `slug` and `published_at`.

## 2. Configure secrets

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and place your project URL and a **service_role key** in it. The service-role key must never be placed in browser code, Git, or a public deployment. The supplied publishable key is suitable only if RLS policies explicitly allow every needed write.

Add the admin login credentials to the same file:

```toml
ADMIN_USERNAME = "your-admin-username"
ADMIN_PASSWORD = "your-admin-password"
```

The dashboard allows five failed login attempts per day. Failed attempts are stored in Supabase, so refreshing or reopening the browser does not reset the daily lock. Run `setup.sql` after adding this login feature so the `admin_login_attempts` table exists.

Successful logins are remembered for 30 days in a browser cookie. The cookie contains a random session token, not the username or password; the token hash and expiration are stored in the `admin_login_sessions` table created by `setup.sql`.

## 3. Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Open the local URL Streamlit prints. For deployment, add the same two secrets in the hosting provider's secret manager.

## Notes

- The Yahoo importer upserts by `(market_id, timestamp)`, so rerunning a period updates instead of duplicating it.
- The article editor intentionally previews raw HTML. Treat dashboard access and article authors as trusted; sanitize HTML before rendering it publicly if non-admin users can supply content.
- If your column names differ, use **Table browser** to insert/update/delete rows with JSON, or rename the fields in `app.py`.

## Connect your live website (read-only)

Use [`frontend-supabase.js.example`](frontend-supabase.js.example) in your website. Add the URL and **publishable** key as public frontend environment variables; it is designed for browser use and must never receive the dashboard's secret/service-role key.

The public website needs read-only RLS policies. Review and run the following only after confirming the table/column names:

```sql
alter table public.market_history enable row level security;
alter table public.research_articles enable row level security;

create policy "Public can read market history"
on public.market_history for select to anon using (true);

create policy "Public can read published articles"
on public.research_articles for select to anon
using (published_at is not null and published_at <= now());
```

Do not add public `insert`, `update`, or `delete` policies. Keep all writing in this private Streamlit admin application.
