FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Root is the only user in this image, so pip's venv advice does not apply.
ENV PIP_ROOT_USER_ACTION=ignore

COPY requirements.txt .
# pip carries vendored copies of msgpack and setuptools that Trivy reports as
# HIGH (they live in pip/_vendor/vendor.txt, not in anything we import). The
# relay never installs anything at runtime, so pip leaves with them; setuptools
# is upgraded first because that one is a real installed package.
RUN pip install --no-cache-dir --upgrade setuptools \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip

COPY parser.py relay.py chat_id.py setup_env.py test_parser.py ./

# The Telethon session lives here so the login survives container restarts.
VOLUME ["/app/session"]
ENV SESSION_NAME=/app/session/lexx_relay

CMD ["python", "relay.py"]
