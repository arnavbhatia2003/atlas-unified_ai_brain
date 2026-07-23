# Deploying Atlas

Three pieces: a **database** (Supabase), a **backend** (pick one host below), and
a **frontend** (Vercel). Do step 1, one option in step 2, then steps 3–5.

| Piece | Host | Cost / payment |
|---|---|---|
| Database | **Supabase** free | Free |
| Backend — easiest | **Railway** | ~$5/mo, **card only** |
| Backend — UPI/RuPay | **Hostinger VPS** (or E2E/Utho) | Paid, **UPI + RuPay** |
| Backend — no card | **Render** free | Free (PDFs text-only, cold starts) |
| Frontend | **Vercel** free | Free |
| LLM / embeddings | **NVIDIA NIM** free | Free |

Backend options compared:

| | Railway | Hostinger VPS | Render free |
|---|---|---|---|
| Full Docling PDFs | Yes | Yes | No (text-only) |
| Cold start | No | No | Yes (sleeps) |
| HTTPS | Built-in | via Caddy+DuckDNS | Built-in |
| Pay by | Card | **UPI / RuPay** | — (free) |

---

## 1. Database — Supabase (free)
1. Create a project at [supabase.com](https://supabase.com) → note the DB password.
2. **SQL Editor** → paste the full contents of `backend/app/db/schema.sql`
   (it starts with `create extension if not exists vector;`) → **Run**.
3. **Project Settings → Database → Connection string → URI** (the pooler URI) →
   this is your `DATABASE_URL`.

---

## 2A. Backend on Railway (easiest — card)
Railway builds the root `Dockerfile` and serves it on its own HTTPS domain.
`railway.toml` is already in the repo (Dockerfile builder, health check, 1 replica).

1. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
   → pick `atlas-unified_ai_brain`. It auto-detects the Dockerfile and builds
   (~10–15 min; torch + Docling models bake in).
2. **Variables** (service → Variables) → add:
   - `DATABASE_URL` (from step 1)
   - `NVIDIA_API_KEY`
   - `EMBEDDING_DIM` = `2048`
   - `DOCLING_ENABLED` = `true`
   - `FRONTEND_ORIGIN` = your Vercel URL (fill after step 3)
3. **Settings → Networking → Generate Domain** → you get
   `https://<name>.up.railway.app`. Verify `…/api/health` → `healthy`.
4. If the build/runtime OOMs, raise the service memory in Settings (Hobby allows
   up to 8 GB; Docling needs ~2 GB).

> Railway is **card-only** and has no free tier (Hobby ≈ $5/mo). If your card is
> rejected, use option 2B (Hostinger, UPI/RuPay).

## 2B. Backend on Hostinger VPS (UPI / RuPay)
You get a full Ubuntu VPS with a public IP; the repo's `deploy/` stack (backend +
Caddy for auto-HTTPS) runs on it. Same steps work on **E2E Networks** or **Utho**
(also UPI) or any Ubuntu VPS.

1. **Buy a VPS:** Hostinger → VPS → **KVM 1** (4 GB RAM — enough for Docling) or
   larger. Pay by **UPI or RuPay**. Choose OS **Ubuntu 24.04** (or Hostinger's
   "Ubuntu + Docker" template to skip the Docker install below). Note the
   **public IP** and set a root/SSH password in hPanel.
2. **Free HTTPS domain:** at [duckdns.org](https://www.duckdns.org) create a
   subdomain (e.g. `atlas-demo`) and point it to the VPS public IP.
3. **Open ports 80 + 443:** in hPanel's VPS **Firewall** (if enabled) allow TCP
   80 and 443. (Hostinger usually leaves them open; no separate cloud firewall
   like Oracle.)
4. **SSH in** (`ssh root@<public-ip>`) and run:
   ```bash
   # Docker (skip if you picked the Docker OS template)
   apt-get update && apt-get install -y docker.io docker-compose-plugin git

   git clone https://github.com/arnavbhatia2003/atlas-unified_ai_brain.git
   cd atlas-unified_ai_brain
   cp backend/.env.example backend/.env
   nano backend/.env      # set DATABASE_URL, NVIDIA_API_KEY; DOCLING_ENABLED=true

   DOMAIN=atlas-demo.duckdns.org docker compose -f deploy/docker-compose.yml up -d --build
   ```
   First build ~10–15 min. Verify `https://atlas-demo.duckdns.org/api/health`.
5. After step 3 (frontend) set `FRONTEND_ORIGIN` in `backend/.env` to the Vercel
   URL and re-run the `docker compose ... up -d` line.

> x86 VPS (Hostinger/E2E/Utho) is actually easier than Oracle's ARM — torch's
> x86 wheels are rock-solid. **E2E/Utho** bill prepaid: top up a small amount by
> UPI, run an 8 GB node for the demo, destroy it after.

## 2C. Backend on Render (free, no card)
Deploys the root `Dockerfile` via `render.yaml`. Free = 512 MB, so PDFs run
**text-only** (`DOCLING_ENABLED=false`, set for you in `render.yaml`) and the
service **sleeps when idle** (cold start on wake).

1. [render.com](https://render.com) → **New → Blueprint** → pick the repo.
2. Set `DATABASE_URL`, `NVIDIA_API_KEY`, `FRONTEND_ORIGIN` in the dashboard.
3. You get `https://<name>.onrender.com`; verify `…/api/health`.

---

## 3. Frontend — Vercel (free)
1. [vercel.com](https://vercel.com) → **Add New → Project** → import the repo.
2. **Root Directory: `frontend`** (`vercel.json` handles the SPA rewrite).
3. **Environment Variables** → `VITE_API_URL =` your backend URL from step 2
   (the Railway / DuckDNS / Render HTTPS URL).
4. Deploy → `https://<project>.vercel.app`.

## 4. Wire CORS
Set the backend's `FRONTEND_ORIGIN` to your exact Vercel URL, then redeploy /
restart the backend. (Railway/Render: update the variable; Hostinger: edit
`backend/.env` and re-run compose.) Extra origins: `FRONTEND_ORIGINS`
(comma-separated). localhost stays allowed for dev.

## 5. Seed the demo
Add the five connectors in [`demo/connectors/`](demo/connectors/) from the
Connectors page (each as a `manual` connector, then Sync), and follow
[`demo/DEMO_SCRIPT.md`](demo/DEMO_SCRIPT.md).

---

## Notes
- **Single worker** is required everywhere (ingest job registry + DB pool assume
  one process). Don't add `--workers` or extra replicas.
- **`DOCLING_ENABLED`**: `true` on Railway/VPS (≥2 GB RAM, full tables+layout);
  `false` on Render free (text-only, won't OOM).
- **NVIDIA NIM** free tier = 40 req/min shared across all calls; a public URL
  means anyone can consume the budget (fine for a judged demo).
- **Supabase free** pauses after ~7 days idle — open the dashboard to resume.
- **Watch add-on charges** on prepaid Indian clouds (e.g. a public-IP line item);
  check the bill breakdown.
