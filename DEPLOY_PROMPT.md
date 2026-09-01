# Claude Code prompt — deploy WeatherGPT

Paste everything in the block below into Claude Code, run from
`C:\Users\udayp\Hackathon\weathergpt`.

Pick your platform first and replace `<PLATFORM>` on the first line.
Recommended: **Hugging Face Spaces (Docker SDK)** — free, no card, and it does
not sleep, which matters because a judge will not wait 50 s for a cold start.
Alternatives: **Fly.io** (always-on, needs a card) or **Render** (easiest, but
the free tier sleeps — only pick it if you add the keep-warm cron in step 7).

---

```text
Deploy this FastAPI app to <PLATFORM> and give me a working public URL.

CONTEXT
This is WeatherGPT, a Smart India Hackathon prototype (PS SIH26068, judged
20 Sep). It is a FastAPI backend serving a single-file HTML frontend from
/frontend, plus a WebSocket endpoint at /ws/alerts. Read README.md first —
its "Honest limits" section lists constraints that affect deployment. It runs
with zero API keys configured.

The demo is the product here. If the URL is slow to wake, drops the
WebSocket, or loses alert subscriptions between two requests, the deployment
has failed even if the container is green.

FIX THESE BEFORE DEPLOYING — both are live bugs, verify each one
1. Dockerfile CMD hardcodes `--port 8000`. Every managed platform injects
   $PORT. Change to a shell-form CMD or an entrypoint script that honours
   $PORT and defaults to 8000. The HEALTHCHECK hardcodes localhost:8000 too —
   make it use the same variable.
2. Dockerfile runs `--workers 2`. Alert subscriptions and the delivery log
   live in a module-level dict in backend/app/alerts.py, so with two workers a
   POST /api/alerts/subscribe and a POST /api/alerts/simulate can land on
   different processes and match zero subscribers. Force a single worker and
   put a comment above it saying why, so nobody "optimises" it back.
3. Add a .dockerignore (.venv, __pycache__, .git, *.pyc, tests, .env) and
   confirm no .env or secret is in the image or the repo.

REQUIREMENTS
4. CORS in backend/app/main.py is currently allow_origins=["*"]. Restrict it
   to the deployed origin, read from an env var, defaulting to "*" only when
   unset so local dev still works.
5. Protect the demo endpoints. /api/alerts/simulate broadcasts a synthetic RED
   cyclone alert to every matching subscriber; /api/alerts/subscribe accepts
   any phone number with no verification. Nothing actually sends SMS in this
   build (delivery is a log), so this is a defacement/abuse risk rather than a
   safety one today — but it becomes a real one the moment a channel is wired.
   Add a shared-secret header check (env var DEMO_TOKEN) on simulate,
   subscribe and scan, and make the frontend send it from a value injected at
   page load. Keep ENABLE_DEMO_ENDPOINTS=true — I need simulate working live
   on stage — and leave the "simulated": true marker in the response.
6. Add light rate limiting on /api/chat and /api/alerts/subscribe (in-process
   is fine, no Redis). The upstream weather API rate-limits by IP and every
   request from the deployment shares one, so one scraper could exhaust the
   quota mid-demo.
7. Platform config: healthcheck on /api/health, restart on failure, and if the
   platform sleeps idle instances, add a cron or uptime ping every 10 minutes
   and tell me it is there.
8. Check the platform's CURRENT docs for Docker deployment, port handling,
   WebSocket support and free-tier terms before writing any config — do not
   rely on what you remember. WebSocket support is a hard requirement; if the
   platform cannot do it, stop and tell me before deploying.

ACCEPTANCE — run these against the deployed URL and paste the real output
  - GET  /api/health returns 200
  - POST /api/chat {"message":"Weather in Hyderabad right now"} returns a
    temperature with a non-empty "sources" array
  - POST /api/chat {"message":"kal Guntur me barish hogi kya?","lang":"hi"}
    returns Devanagari text
  - GET  /api/parse?q=weather+in+Puri returns an intent and NO weather value
  - Subscribe a farmer at Puri, then simulate a red alert at Puri, and confirm
    "matched": 1 with a Telugu message in the delivery log — this is the proof
    that single-worker state works
  - The WebSocket at /ws/alerts accepts a connection and replies to
    {"action":"ping"}
  - Open the root URL and confirm the chat UI loads and answers a question
  - Report cold-start time and the p50 latency of a repeated /api/chat call

DO NOT
  - Do not commit secrets, .env files, or a DEMO_TOKEN value to the repo.
  - Do not switch to a serverless/edge runtime — it breaks the WebSocket and
    the in-memory subscription state.
  - Do not change any threshold in backend/app/advisory.py, any provenance
    field, or anything in backend/app/nlu.py. Those encode the project's
    correctness claims and are out of scope for a deploy.
  - Do not "fix" the in-memory store by adding a database in this pass. Note
    it as follow-up work instead.

When done: give me the URL, the exact command to redeploy after a code change,
and a short list of anything you changed beyond the items above.
```

---

## After it deploys

Two follow-ups worth doing before the 20 Sep submission, in this order:

1. **Put the live URL on slide 1 of the deck** and in the SIH portal
   submission. A judge who can open it before the presentation arrives
   already convinced.
2. **Warm the cache for your demo cities on startup** — Hyderabad, Guntur,
   Warangal, Nizamabad, Nagapattinam, Puri, Cherrapunji, Delhi. A prefetch on
   boot turns your first on-stage query from ~1 s into ~5 ms and removes the
   one moment where a rate-limited upstream could embarrass you.
