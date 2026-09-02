# WeatherGPT

**SIH 2026 · Problem Statement SIH26068** — Ministry of Earth Sciences /
India Meteorological Department · Category: Software · Theme: Disaster Management

Conversational weather intelligence for India: real-time conditions, NWP
forecasts, impact-based warnings, sector decision support, climate trends and
multilingual voice — with a hard guarantee that no number is ever generated
by a language model.

---

## Run it

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000> for the chat UI, <http://localhost:8000/docs> for
the OpenAPI reference.

Nothing needs configuring. With no API keys at all, the service runs on open
NWP output, the rule-based router and bundled language templates. Add keys in
`backend/.env` (see `.env.example`) to upgrade each layer independently.

```bash
# 38 checks: routing, grounding, thresholds, dissemination, cache latency.
# The suite drives /api/chat faster than a human, so start the server with
# RATE_LIMIT_CHAT_PER_MIN=0; export DEMO_TOKEN too if the server has one set.
python3 tests/test_smoke.py
docker compose up --build          # containerised stack
```

---

## What makes it different

Most weather chatbots are `LLM + weather API`. Three things here are not.

### 1. The LLM cannot state a weather value

The model is a **router**, not a source. It converts free text into a typed
`ParsedQuery` and never sees raw data with a "what's the weather" instruction.
Every number comes from a provider, is wrapped in a `Fact` carrying a
`Provenance` record (source, product, issue time, whether it is authoritative),
and is rendered by a deterministic template.

If an optional LLM rewrite is enabled, `nlu.numerals_preserved()` compares the
numeral multiset before and after and discards any rewrite that added, dropped
or altered a figure. A hallucinated rainfall total is structurally impossible,
not merely unlikely.

The UI shows the provenance chips on every answer. Green chip = IMD
(authoritative). Blue chip = model-derived. A user always knows which they are
reading.

### 2. It answers the decision, not the variable

`32 °C, 18 mm, gusts 45 km/h` is data. `Do not spray today — the rain will wash
it off; next window Thursday morning` is the product. `advisory.py` encodes
IMD's own impact thresholds as auditable rules per sector:

| Sector | Example output |
|---|---|
| Farmer | spray/no-spray window, irrigation skip, sowing window, disease pressure |
| Fisherman | go / no-go against the 34 kt small-craft threshold, next workable window |
| Aviation | surface wind in kt, crosswind and RVR flags, contaminated-runway warning |
| City / DM | waterlogging risk, pump pre-positioning, heat action plan trigger |

Every advisory reports the variable and threshold that fired, so "why?" has a
real answer.

### 3. It pushes, not only pulls

A chatbot helps whoever thought to ask. Early warning has to reach whoever did
not. `alerts.py` is a subscribe → geofence-match → per-subscriber render →
multi-channel fan-out engine. One incoming alert becomes a Telugu SMS to a
farmer, a Tamil IVR call to a fisherman and an English push to a district EOC —
the same event, rendered three ways by role and language.

---

## Multilingual, done safely

Machine-translating a finished warning sentence reorders numerals and softens
imperative verbs. A mistranslated *do not put to sea* is a fatality.

So WeatherGPT translates the **structure**: an answer is a template id plus a
slot dict, and each language owns its own template with the same slots. Numbers,
units and place names are injected after translation and never pass through an
MT model. Six languages ship with bundled templates (en, hi, te, ta, bn, mr);
adding a seventh is a data change, not a code change.

Bhashini (MeitY) handles the long tail — ASR for speech input, NMT for
free-text questions with no template, TTS for output — and is optional.

---

## Data sources

| Layer | Source | Status in this build |
|---|---|---|
| Authoritative obs, nowcast, warnings, cyclone, marine | **IMD public API** (`api.imd.gov.in`, 20 endpoints mapped in `providers/imd.py`) | needs a free key + IP whitelisting — [register](https://api.imd.gov.in/public/register.php) |
| NWP forecast | NCEP GFS 0.25° / ECMWF IFS / ICON | live |
| Reanalysis / climate | ERA5 daily archive, 1940– | live |
| Air quality | CAMS composition, banded to the CPCB National AQI scale | live |
| Alert exchange | WMO **WIS2.0** MQTT Global Broker, CAP 1.2 | interface built, broker not subscribed in the demo |
| Language | Bhashini ASR/NMT/TTS + bundled templates | templates live, Bhashini optional |

`GET /api/imd/endpoints` lists the full IMD integration surface, so the wiring
is visible before a key is issued.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat` | the conversational endpoint |
| GET | `/api/parse` | router output only — proves the LLM emits an intent, not a value |
| GET | `/api/weather/current`, `/api/weather/forecast` | typed data access |
| GET | `/api/climate/trend` | OLS trend over the ERA5 archive |
| POST | `/api/alerts/subscribe` · `/scan` · `/simulate` | dissemination engine |
| GET | `/api/alerts/log` · `/subscriptions` | delivery audit |
| WS | `/ws/alerts` | live push channel |
| GET | `/api/health` | source status, cache hit rate, degradation flags |

---

## Latency

Two-tier cache, TTL pinned to the data's own validity rather than a fixed
number; independent upstream calls issued concurrently; decade-chunked archive
requests so a 40-year query reuses three quarters of a 30-year one.

Measured on this build: **~1.0 s cold, <5 ms cached** for a current-conditions
query. In deployment, precomputed per-district answer tiles refreshed on model
arrival take the common queries off the request path entirely.

---

## Layout

```
backend/app/
  main.py        FastAPI surface, degradation handling
  nlu.py         rule router + optional LLM router + numeral guard
  tools.py       deterministic answer builders (one per intent)
  advisory.py    IMD impact thresholds → sector decisions
  i18n.py        slot templates per language + Bhashini adapter
  alerts.py      subscribe / geofence / render / fan-out
  cache.py       two-tier TTL cache
  schemas.py     typed contracts, incl. Provenance
  providers/     imd.py · openmeteo.py · geocode.py
frontend/
  index.html     single-file chat UI, voice, canvas charts, no dependencies
tests/
  test_smoke.py  38 checks
```

## Deploying to Render

Docker web service on the free plan, configured by `render.yaml`
([Blueprint](https://render.com/docs/blueprint-spec)). First deploy: create a
Blueprint from the repo in the Render dashboard; it reads `render.yaml` and
provisions the service.

Redeploy after a code change:

```bash
git add -A && git commit -m "your change" && git push origin main
```

`autoDeployTrigger: commit` rebuilds on every push to the tracked branch.

Nothing needs setting by hand. `DEMO_TOKEN` is generated by Render
(`generateValue: true`) and never leaves the dashboard; `CORS_ALLOW_ORIGINS` is
left unset on purpose so the app pins CORS to `RENDER_EXTERNAL_URL`, the
service's own URL, which Render injects. `ENABLE_DEMO_ENDPOINTS` stays `true`
so `/api/alerts/simulate` works on stage.

Three platform facts the config depends on:

- **Render injects `$PORT`** (default `10000`). The Dockerfile's shell-form
  `--port "${PORT:-8000}"` honours it; the `8000` default is only for local
  runs. Do not pin `PORT` in `render.yaml`.
- **The app must bind `0.0.0.0`**, which the Dockerfile does.
- **WebSockets are supported**, so `/ws/alerts` and the live push channel in
  the UI work. The UI still falls back to HTTP if the upgrade is blocked.

`healthCheckPath: /api/health` gates each deploy and triggers a restart when an
instance stops passing.

### Keep-alive: turn it on for demo dates only

A free service **spins down after 15 minutes without inbound traffic** and
takes about a minute to wake, which is exactly the cold start a judge should
never see. `.github/workflows/keepalive.yml` pings `/api/health` every 10
minutes to prevent that.

Leave it **disabled except around demo dates.** The free plan grants **750
instance-hours per workspace per calendar month**, and a service kept
permanently awake burns about **730 of them** — roughly 97% of the allowance
for this one service, with nothing left for a second. Run it continuously all
month and the service will be suspended partway through the next one.

Enable it a day before a demo and disable it afterwards:

```bash
gh workflow enable  keepalive.yml    # before the demo
gh workflow disable keepalive.yml    # after
```

It needs a `SPACE_URL` repository variable set to the service URL
(`https://<service>.onrender.com`). No token: `/api/health` is ungated.

## Upstream sources and failover

`api.open-meteo.com` rate-limits by source IP. A free managed host shares one
egress IP across tenants, so on Render it returns **429 to every forecast
request** — the quota is spent by other tenants and no amount of retrying
recovers it. Current conditions, forecast, warnings and advisory all depend on
that call; air quality and the ERA5 archive are different hosts and are
unaffected.

`providers/nwp.py` therefore tries providers in order and fails over:

| Order | Provider | Notes |
|---|---|---|
| 1 | Open-Meteo (GFS/ECMWF/ICON) | primary; **429 is never retried** |
| 2 | MET Norway Locationforecast 2.0 | independent model chain and network path |

The `Provenance` record names whichever source answered, so the chip in the UI
visibly changes on failover and no number is ever misattributed.

Two caveats when MET Norway is answering:

- **No wind gusts for most Indian points.** `advisory.py` already falls back to
  sustained wind, which is lower — so the 34 kt small-craft threshold fires
  less readily. Gust-driven advice is weaker, not wrong.
- **No probability of precipitation**, rendered as an em dash rather than a
  fabricated percentage.

Set `METNO_CONTACT`; MET Norway requires a reachable contact in the
User-Agent. Each request has a total upstream budget (`UPSTREAM_BUDGET_S`,
default 12s) across all providers, so a dead upstream degrades one answer
instead of starving the instance.

## Follow-up work

- **Move `SUBSCRIPTIONS` and `DELIVERY_LOG` out of process memory.** They are
  module-level dicts in `alerts.py`, which is why the Dockerfile pins
  `--workers 1` and why the deployment cannot scale horizontally or survive a
  restart. Redis or the Postgres/PostGIS service already in
  `docker-compose.yml` is the destination. Deliberately not done as part of
  the deploy pass.
- Replace the injected demo token with a server-side session or a signed,
  short-lived token before wiring a real SMS/IVR channel.
- Rate limiting is in-process, so it is per-container. It becomes per-user
  only alongside the shared store above.

## Honest limits

Read this section before demoing. Every item here is something a judge could
find, so it is better said first.

- **IMD adapter is mapped, not exercised.** All 20 endpoints are transcribed
  from the published reference, but nothing has run against a live key — that
  needs a registered, whitelisted IP. The district-warning call additionally
  needs IMD's district *object id*; `imd.resolve_district_id()` returns `None`
  until that master list is loaded, and the answer reports the gap in
  `degraded` rather than guessing an id and returning the wrong district.
  Consequence for the demo: **the green "authoritative / IMD" provenance chip
  cannot appear yet.** Everything you can show today is model-derived and
  labelled as such.
- **Heat rules are screening thresholds, not IMD's heat-wave criterion.** IMD
  defines a heat wave by departure from the station normal (≥4.5 °C, ≥6.4 °C
  severe), not by an absolute temperature. This build uses 40 °C / 45 °C
  absolute screens and says so in the reason text. The ERA5 archive the
  climate module already reads can supply the normals; until it does, do not
  claim the real criterion.
- **WIS2.0 MQTT ingest is designed and interfaced, not subscribed.**
- **`/api/alerts/simulate` broadcasts a synthetic red alert.** It is gated
  behind `ENABLE_DEMO_ENDPOINTS` (default true for the demo) and every event
  it produces is stamped `"simulated": true`. Set it false on anything
  reachable from outside — an open endpoint that fans out a fabricated
  national warning is a public-safety hazard.
- **The dissemination endpoints are gated by a shared secret, not by auth.**
  `/api/alerts/simulate`, `/subscribe`, `/scan` and the `/ws/alerts` upgrade
  require an `X-Demo-Token` matching `DEMO_TOKEN`. The gate **fails closed**:
  if `ENABLE_DEMO_ENDPOINTS` is true and `DEMO_TOKEN` is unset, those routes
  return 503 and the socket refuses the upgrade, rather than opening up. An
  earlier build treated "unset" as "open", which is how a deployed instance
  ended up accepting `/simulate` from the public internet. CORS is pinned to
  `CORS_ALLOW_ORIGINS`, and `/api/chat` and `/subscribe` are rate-limited
  per IP in-process.
  Be precise about what that is worth: **the token is injected into the page at
  load, so anyone who can open the UI can read it.** It stops drive-by scripted
  abuse; it is not access control and would not stop a determined attacker.
  `/api/alerts/subscribe` still accepts any address with no verification.
  Because delivery in this build is a log write and nothing sends SMS, today's
  exposure is defacement and log-flooding rather than a safety one — but a real
  session or signed short-lived token must replace this before any SMS/IVR
  channel is wired in.
- **Subscriptions and the delivery log are in memory** and vanish on restart.
- **Agromet advice needs domain validation** against IMD's own AAS bulletins
  before anyone treats it as guidance.
- **Forecast skill is the NWP model's.** WeatherGPT does not improve the
  forecast; it makes it reachable, actionable, multilingual and attributable.
