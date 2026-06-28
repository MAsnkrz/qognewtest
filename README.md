# Qogita Category Monitor — Webhook System Setup

This system monitors the **Makeup** and **Health** categories on Qogita
(whole category, all brands) for new arrivals, price drops, and
back-in-stock — something only possible via the async catalog-download
+ webhook flow, since category filtering doesn't work on the
synchronous API endpoints yet.

## How it fits together

```
[Scheduled GH workflow]  --POST-->  Qogita (async job starts)
                                         |
                                         | (a few minutes later)
                                         v
                              Qogita calls your webhook URL
                                         |
                                         v
                          [Cloudflare Worker] verifies signature
                                         |
                                         | repository_dispatch
                                         v
                      [GH workflow] downloads CSV, alerts Discord
```

Two independent loops run per category (Makeup, Health):
1. A **trigger workflow** fires every 2 hours — just starts the job, does nothing else.
2. A **process workflow** sits idle until the Cloudflare Worker wakes it up via `repository_dispatch` — that's when the actual CSV gets downloaded and compared against the snapshot.

## Files in this bundle

| File | Purpose | Goes where |
|---|---|---|
| `cloudflare_worker.js` | Webhook receiver — verifies signature, dispatches to GitHub | Cloudflare Worker |
| `wrangler.toml` | Worker deploy config | Same folder as the Worker JS |
| `register_qogita_webhook.py` | One-time: registers the Worker URL with Qogita | Run locally once |
| `list_qogita_webhooks.py` | Debug helper: list endpoints, send test event | Run locally as needed |
| `qogita_catalog_trigger.py` | Fires the async download request | GitHub repo root |
| `qogita_trigger_makeup.yml` / `qogita_trigger_health.yml` | Scheduled triggers | `.github/workflows/` |
| `qogita_makeup_category_monitor.py` / `qogita_health_category_monitor.py` | Parses CSV, alerts Discord | GitHub repo root |
| `qogita_process_makeup.yml` / `qogita_process_health.yml` | `repository_dispatch`-triggered processors | `.github/workflows/` |

## Setup steps

### 1. Create the GitHub repo
Create a new private repo. Add all the `.py` files to the root, and all the `.yml` files into `.github/workflows/`.

Add these **repository secrets** (Settings → Secrets and variables → Actions):
- `QOGITA_EMAIL`
- `QOGITA_PASSWORD`
- `DISCORD_WEBHOOK_MAKEUP` (your Makeup alerts channel webhook)
- `DISCORD_WEBHOOK_HEALTH` (your Health alerts channel webhook — can be the same channel/webhook as Makeup if you prefer one feed)

### 2. Create a GitHub Personal Access Token
This is what the Cloudflare Worker uses to wake up your repo's workflows.

- Go to GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
- Create a new token scoped to **only this repository**
- Permissions needed: **Contents: Read and write**, **Actions: Read and write** (repository_dispatch needs this)
- Copy the token — you'll paste it into Cloudflare in step 4, never into chat or the repo itself

### 3. Deploy the Cloudflare Worker
1. Sign up free at [dash.cloudflare.com](https://dash.cloudflare.com) if you don't have an account
2. Easiest path — no CLI needed:
   - Workers & Pages → Create → Create Worker
   - Give it a name (e.g. `qogita-webhook-receiver`) — note the resulting URL, like `https://qogita-webhook-receiver.YOURNAME.workers.dev/`
   - Click **Edit code**, delete the placeholder, paste in the full contents of `cloudflare_worker.js`
   - Click **Deploy**
3. Set the secrets (Settings → Variables and Secrets → Add):
   - `GITHUB_PAT` → the token from step 2
   - `GITHUB_OWNER` → your GitHub username
   - `GITHUB_REPO` → the repo name from step 1
   - `QOGITA_SIGNING_SECRET` → leave blank for now, you'll get this in step 4

*(If you prefer the CLI: `npm install -g wrangler`, then `wrangler login`, then run `wrangler deploy` from this folder, and `wrangler secret put <NAME>` for each secret above.)*

### 4. Register the webhook with Qogita
Edit `register_qogita_webhook.py` — set `WORKER_URL` to your real deployed Worker URL from step 3.

Run it locally:
```bash
pip install requests
python3 register_qogita_webhook.py
```

It prints a `signingSecret` — **copy it immediately, it's shown only once.** Go back to the Cloudflare Worker secrets and set `QOGITA_SIGNING_SECRET` to that value.

### 5. Test the connection
```bash
python3 list_qogita_webhooks.py --test
```

In another terminal, watch the Worker's live logs:
```bash
wrangler tail qogita-webhook-receiver
```
*(Or just check the Worker's "Logs" tab in the Cloudflare dashboard if you're not using the CLI.)*

You should see the `webhook.test` event arrive and pass signature verification. If it fails, double check `QOGITA_SIGNING_SECRET` was pasted correctly with no extra whitespace.

### 6. Build the baseline
In your GitHub repo → Actions tab:
- Run **"Qogita Makeup Catalog Trigger"** manually (workflow_dispatch)
- Run **"Qogita Health Catalog Trigger"** manually (workflow_dispatch)

Each one fires the async job. A few minutes later, Qogita's webhook should hit your Worker, which dispatches to GitHub, which runs the matching **Processor** workflow — this first run builds the baseline silently (no Discord alerts), same pattern as your other monitors.

### 7. You're live
From here on:
- The trigger workflows run automatically every 2 hours
- Each completed download flows through the Worker → triggers the processor → Discord alerts fire for genuine new arrivals, price drops (tiered colours, % shown), and back-in-stock — exactly like your brand monitors, but covering the entire category across all ~2,500+ brands.

## Adjusting the schedule
Both trigger workflows currently run every 2 hours (`cron: "0 */2 * * *"` and `"5 */2 * * *"`). Qogita allows 3 of these requests per minute, so you have plenty of headroom to go more frequent if you want — just edit the cron lines.

## Troubleshooting
- **Worker logs show nothing** → check the webhook URL registered with Qogita exactly matches your deployed Worker URL (trailing slash included)
- **"Invalid signature" in Worker logs** → `QOGITA_SIGNING_SECRET` doesn't match; delete the webhook endpoint and re-run `register_qogita_webhook.py` to get a fresh one
- **GitHub workflow never fires** → check `GITHUB_PAT` has Actions: Read & write permission, and `GITHUB_OWNER`/`GITHUB_REPO` are exactly right
- **`400 no_webhook_subscriber`** when triggering → the webhook endpoint isn't both *registered* and *enabled* and *subscribed* to both event types — check with `list_qogita_webhooks.py`
