FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Hugging Face Spaces runs the container as UID 1000. Create that user before
# any COPY and own every copied file, otherwise the app cannot read its own
# code. https://huggingface.co/docs/hub/spaces-sdks-docker#permissions
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR $HOME/app

COPY --chown=user backend/requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY --chown=user backend/ ./backend/
COPY --chown=user frontend/ ./frontend/

WORKDIR $HOME/app/backend

# $PORT is honoured whenever the platform injects one; 8000 is the local
# default. Note that HF Spaces does NOT inject $PORT -- it routes to the port
# declared as `app_port:` in the README.md YAML block, which is pinned to 8000
# to match this default. Keep the two in sync.
ENV PORT=8000
EXPOSE 8000

# Same variable as the CMD, so the check follows the port the app actually
# bound to instead of a hardcoded 8000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health')"

# --- ONE WORKER. DO NOT RAISE THIS. -----------------------------------------
# Alert subscriptions and the delivery log are module-level dicts in
# backend/app/alerts.py (SUBSCRIPTIONS, DELIVERY_LOG). Each uvicorn worker is a
# separate process with its own copy, so with --workers 2 a
# POST /api/alerts/subscribe and a POST /api/alerts/simulate can land on
# different processes and the simulate will match ZERO subscribers -- the
# dissemination demo silently shows nothing.
# This is not a performance setting; it is a correctness one. It can only be
# raised after SUBSCRIPTIONS/DELIVERY_LOG move to a shared store (Redis or
# Postgres). See "Follow-up work" in README.md.
# Shell form (not exec form) so ${PORT} is expanded by the shell at runtime.
CMD uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
