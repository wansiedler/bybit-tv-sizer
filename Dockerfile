FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parser.py relay.py chat_id.py setup_env.py test_parser.py ./

# The Telethon session lives here so the login survives container restarts.
VOLUME ["/app/session"]
ENV SESSION_NAME=/app/session/lexx_relay

CMD ["python", "relay.py"]
