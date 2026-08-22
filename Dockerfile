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
# relay never installs anything at runtime, so pip leaves with them; setuptools
# is upgraded first because that one is a real installed package.
RUN pip install --no-cache-dir --upgrade --only-binary :all: setuptools \
    && pip install --no-cache-dir --require-hashes --only-binary :all: --no-binary pyaes \
         -r requirements.lock \
    && pip uninstall -y pip

COPY parser.py relay.py speaker.py commands.py chat_id.py setup_env.py ./

# The Telethon session lives here so the login survives container restarts.
VOLUME ["/app/session"]
ENV SESSION_NAME=/app/session/lexx_relay

# Nothing here needs root. uid 1000 owns the app dir, the session volume it
# writes the login into, and the cache directory the speech files go to.
RUN useradd --create-home --uid 1000 relay \
    && mkdir -p /app/session /home/relay/.cache/lexx-relay/tts \
    && chown -R relay:relay /app /home/relay
USER relay

CMD ["python", "relay.py"]
