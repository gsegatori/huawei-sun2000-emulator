FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ──────────────────── builder ────────────────────
FROM base AS builder

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --user --no-warn-script-location .

# ──────────────────── runtime ────────────────────
FROM base AS runtime

# libcap2-bin per setcap (legare porta 502 senza root)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libcap2-bin && \
    rm -rf /var/lib/apt/lists/*

# Setcap sul binario python cosi' l'utente non-root puo' bindare port 502
RUN setcap 'cap_net_bind_service=+ep' "$(readlink -f $(which python3))"

# Utente non privilegiato
RUN useradd --create-home --shell /bin/bash --uid 1000 app
USER app
WORKDIR /home/app

ENV PATH="/home/app/.local/bin:${PATH}"
COPY --from=builder --chown=app:app /root/.local /home/app/.local
COPY --chown=app:app app /home/app/app

EXPOSE 502 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys,os; \
    p=int(os.environ.get('ADMIN_PORT','5050')); \
    r=urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3); \
    sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-m", "app.main"]
