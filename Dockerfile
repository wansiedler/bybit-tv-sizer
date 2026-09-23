FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Root is the only user in this image, so pip's venv advice does not apply.
ENV PIP_ROOT_USER_ACTION=ignore

COPY requirements.lock .
# --require-hashes: every artifact is verified against requirements.lock, so a
# compromised index cannot swap one out. --only-binary :all: refuses sdists,
# whose setup.py runs at install time — with one exception: pyaes (a telethon
# dependency) publishes no wheel at all. Its sdist is still hash-pinned.
# pip carries vendored copies of msgpack and setuptools that Trivy reports as
# HIGH (they live in pip/_vendor/vendor.txt, not in anything we import). The
# relay never installs anything at runtime, so pip leaves with them — and
# setuptools is never installed at all, so there is nothing left to flag.
RUN pip install --no-cache-dir --require-hashes --only-binary :all: --no-binary pyaes \
        -r requirements.lock \
    && pip uninstall -y pip

COPY parser.py relay.py speaker.py commands.py chat_id.py setup_env.py coin_names.py bybit_watch.py chart.py sizer.py tv_alerts.py sheets.py ip_watch.py ./

# The Telethon session lives here so the login survives container restarts.
VOLUME ["/app/session"]
ENV SESSION_NAME=/app/session/lexx_relay

# Nothing here needs root. uid 1000 owns the app dir, the session volume it
# writes the login into, and the cache directory the speech files go to.
RUN useradd --create-home --uid 1000 relay \
    && mkdir -p /app/session /home/relay/.cache/bybit-tv-sizer/tts \
    && chown -R relay:relay /app /home/relay
USER relay

# Liveness, not readiness: the relay holds the TTS port for the whole of its
# life, so a socket that no longer accepts means the process is wedged rather
# than busy. Trivy DS-0026 asks for this; the reason to want it is that a
# hung relay currently looks identical to an idle one from the outside.
#
# This marks the container unhealthy — it does NOT restart it. `restart:
# unless-stopped` in docker-compose.yml only acts on exit, so a wedged relay
# stays up and unhealthy until something acts on that. Wiring the restart is
# a separate decision.
#
# start-period covers the Telegram login and the first cast handshake, which
# together take a few seconds on a cold start.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.environ.get('TTS_PORT', '8422'))), 3).close()"

CMD ["python", "relay.py"]
