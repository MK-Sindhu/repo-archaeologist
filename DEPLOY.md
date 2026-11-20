# Deploying Repo Archaeologist

Two parts:

- **API** — a Docker container (`Dockerfile`). It clones repositories with `git`, keeps
  their indexes on disk and runs each question for one to three minutes while streaming
  progress. That needs a long-running server, so it goes on a container host. The config
  here is for [Render](https://render.com) (`render.yaml`, free plan), and the same image
  runs anywhere Docker does.
- **Website** — a static Vite build, deployed on [Vercel](https://vercel.com) from
  `frontend/` (`frontend/vercel.json`). It talks to the API at the URL you give it.

The API container also serves the website, so the Vercel step is optional.

Everything is configured with environment variables on the hosts; nothing secret is in
the repository or the image.

## 0. Push the repository to GitHub

Render and Vercel deploy from a Git repository (a private one is fine). `.env` is git-ignored,
so your local keys stay on your machine.

## 1. API on Render

1. In Render: **New → Blueprint**, connect your GitHub account, and pick the repository.
   Render reads `render.yaml`.
2. Fill in the values it asks for:
   - `GEMINI_API_KEY` and/or `ANTHROPIC_API_KEY` — at least one. With only a Gemini key,
     Gemini is used; with both, Claude is.
   - `GITHUB_TOKEN` — optional but recommended (a fine-grained token with **Public
     repositories (read-only)** and no other permissions).
3. Click **Apply**. The first build takes a few minutes.
4. When it's live:
   - Note the service URL, e.g. `https://repo-archaeologist-api.onrender.com`.
   - Open **Environment** and copy the generated `ARCH_ACCESS_CODE`. Visitors need it.
   - Check `https://<your-api>/api/health` returns `{"ok":true}`.

The Render URL already serves the full website. If that's all you need, stop here.

## 2. Website on Vercel

1. In Vercel: **Add New → Project**, import the same repository.
2. Set **Root Directory** to `frontend`. Vercel detects Vite. The build settings come
   from `frontend/vercel.json`.
3. Under **Environment Variables**, add `VITE_API_URL` = your API URL, with no trailing
   slash, e.g. `https://repo-archaeologist-api.onrender.com`.
4. **Deploy**. Open the Vercel URL and enter the access code.

Changing `VITE_API_URL` later needs a redeploy, because it is built into the website.

Optional: allow only your website to call the API by setting `ARCH_CORS_ORIGINS` on Render
to your Vercel URL (e.g. `https://archaeologist.vercel.app`). It defaults to `*`, and the
access code is what protects the API.

## Settings

API (Render → Environment, or `docker run -e`):

| Variable | Container default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | — | LLM keys; at least one |
| `GITHUB_TOKEN` | — | GitHub token for PR/issue lookups |
| `ARCH_ACCESS_CODE` | — (Render generates one) | code visitors must enter; empty = open to anyone |
| `ARCH_PROVIDER` | from the keys | `gemini` or `anthropic` |
| `ARCH_MODEL` / `ARCH_GRADER_MODEL` | provider default | models to use |
| `ARCH_RUNS_PER_HOUR` | `20` | questions per visitor address per hour (`0` = no limit) |
| `ARCH_MAX_REPO_MB` | `500` (Render: `300`) | largest GitHub repository it will clone |
| `ARCH_CORS_ORIGINS` | `*` | websites allowed to call the API (comma-separated) |
| `ARCH_EMBEDDER` | FastEmbed (Render: `none`) | `none` = keyword search only (less memory) |
| `ARCH_WEB_ALLOW_LOCAL` | `0` | the container only opens GitHub repositories |
| `ARCH_HOME` | `/data` | where clones, indexes and question history are kept |
| `PORT` | `8765` | port to listen on (Render sets it) |

Website (Vercel):

| Variable | Meaning |
|---|---|
| `VITE_API_URL` | the API's URL; empty = same origin |

## Free-plan limits worth knowing

- **Render free:**
  - The service sleeps after about 15 idle minutes. The next visit takes up to a minute to
    wake it, and the website shows a "waking up" notice and retries.
  - Its disk is temporary, so cloned repositories and question history disappear on each
    restart or deploy.
  - To keep them, use a paid instance with a disk: uncomment `disk:` in `render.yaml`. It
    mounts at `/data`.
  - With 512 MB of memory, keep `ARCH_EMBEDDER=none` and modest `ARCH_MAX_REPO_MB`.
- **Gemini free tier:** each call takes about 20–40 s, and there is a daily request cap per
  model. One question uses about 4–10 calls.

## Other hosts

The image is a normal web server on `$PORT`:

```bash
docker build -t repo-archaeologist .
docker run -p 8765:8765 -v archaeologist-data:/data \
  -e GEMINI_API_KEY=... -e GITHUB_TOKEN=... -e ARCH_ACCESS_CODE=choose-one \
  repo-archaeologist
```

Fly.io, Railway, Google Cloud Run and a plain VPS all work. On Cloud Run, raise the request
timeout (e.g. `--timeout=3600`), because progress streams for minutes. Use a volume at
`/data` wherever the host offers one.

## Why the API isn't on Vercel

- Vercel Functions stop after 300 s on the Hobby plan.
- They don't keep files between requests, and nothing confirms `git` is available inside
  them.
- Requests can land on different instances, so the in-progress question that a page is
  watching would not be found.

The website is a perfect fit for Vercel; the API needs a server that stays up.
