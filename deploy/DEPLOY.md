# Deploying Daedalus Calculator

Streamlit Community Cloud for the app, Supabase Postgres for the library.
Roughly 20 minutes end to end. Everything below is free-tier.

## 1. Create the database

1. Sign in at <https://supabase.com> and create a project. Keep the region near
   your users; the app loads the whole library on each cold start, so latency
   shows up directly in page-load time.
2. Open **SQL Editor -> New query**, paste `deploy/schema.sql`, run it.
3. Open **Project Settings -> API** and copy two values:
   - **Project URL** -> `SUPABASE_URL`
   - **Project API keys -> `service_role`** -> `SUPABASE_KEY`

`service_role` bypasses row-level security. That is intentional: the app is the
only client, it runs server-side, and it is already behind the password gate.
The key never reaches a browser. Do not use it anywhere that it would.

## 2. Push your current library up

```bash
export SUPABASE_URL=https://xxxx.supabase.co
export SUPABASE_KEY=eyJ...

python scripts/sync_to_supabase.py push --dry-run   # check first
python scripts/sync_to_supabase.py push
```

`push` never deletes. Anything that exists only in the cloud is reported and
left alone.

## 3. Put the code on GitHub

Community Cloud deploys from a GitHub repo, and on the free tier that repo must
be **public**. Your component library does not go with it — that lives in
Supabase — but read `data/` before you push and make sure the seed profiles in
it are ones you are happy to publish.

```bash
git remote add origin git@github.com:<you>/daedalus-calculator.git
git push -u origin main
```

`.gitignore` already excludes `.streamlit/secrets.toml`. Check `git status`
before the first push and confirm it is not listed.

## 4. Deploy

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. **New app** -> pick the repo, branch `main`, main file `app/Home.py`.
3. Open **Advanced settings -> Secrets** and paste:

   ```toml
   app_password = "<the team password>"
   SUPABASE_URL = "https://xxxx.supabase.co"
   SUPABASE_KEY = "eyJ..."
   ```

4. Deploy. First build takes a few minutes while scipy and pandas compile.

Streamlit exposes secrets as environment variables, which is how
`backends.default_backend()` finds Supabase without any deploy-specific code.

## 5. Check it

- The password prompt appears before anything else. If it does not, the
  `app_password` secret is missing or misspelled — the sidebar will say
  *"No password configured"*.
- Home -> **Model provenance** names the store in use. It must say Supabase.
  Local files there means the credentials did not arrive, and any edit you make
  will be written to a container disk that is destroyed on the next redeploy.

## Operating notes

**Backups.** Supabase's free tier keeps daily backups for 7 days. For anything
longer, `python scripts/sync_to_supabase.py pull` writes the whole library back
to `data/` as JSON, which can be committed.

**Concurrent edits.** Writes are last-writer-wins per profile. Two people
editing different components are fine; two people editing the same motor at the
same time means one of them silently loses. There is no locking and no history.
If that becomes a real problem, the fix is an `updated_at` check on write — the
column already exists and is maintained by a trigger.

**Sleeping.** Community Cloud suspends an idle app; the next visitor waits
roughly 30 seconds for it to wake. If that is unacceptable, this is the reason
to move to Render or Fly.

**Cost ceiling.** Free Supabase pauses a project after a week with no activity.
A weekly visit is enough to keep it awake.
