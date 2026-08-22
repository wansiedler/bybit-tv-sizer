"""Interactive .env writer.

Prompts for the credentials and writes them to .env with 0600 permissions.
Secrets are typed here, never passed on the command line, so they stay out of
shell history.
"""

import os
import stat
from getpass import getpass

ENV_PATH = ".env"

FIELDS = [
    ("TG_API_ID", "api_id from my.telegram.org", False),
    ("TG_API_HASH", "api_hash from my.telegram.org", True),
    ("BOT_TOKEN", "token for @asada3289_bot from @BotFather", True),
    ("TARGET_CHAT_ID", "chat id to post into (blank for now)", False),
    ("SOURCE_CHAT", "source chat [lexx_dra_bot]", False),
]


def load_existing() -> dict[str, str]:
    values: dict[str, str] = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def main() -> None:
    values = load_existing()

    for key, hint, secret in FIELDS:
        current = values.get(key, "")
        shown = "already set" if (current and secret) else current or "empty"
        prompt = f"{key} ({hint}) [{shown}]: "
        entered = (getpass(prompt) if secret else input(prompt)).strip()
        if entered:
            values[key] = entered

    values.setdefault("SOURCE_CHAT", "lexx_dra_bot")
    # Inside session/, the directory Docker mounts at /app/session, so a
    # desktop run and a container run share one Telethon login.
    values.setdefault("SESSION_NAME", "session/lexx_relay")

    with open(ENV_PATH, "w", encoding="utf-8") as handle:
        for key in [
            "TG_API_ID",
            "TG_API_HASH",
            "SOURCE_CHAT",
            "BOT_TOKEN",
            "TARGET_CHAT_ID",
            "SESSION_NAME",
        ]:
            handle.write(f"{key}={values.get(key, '')}\n")

    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)

    filled = [k for k, _, _ in FIELDS if values.get(k)]
    print(f"\nwrote {ENV_PATH} (0600). Filled: {', '.join(filled) or 'nothing'}")


if __name__ == "__main__":
    main()
