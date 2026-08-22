"""Tests for the two one-shot setup scripts.

`chat_id.py` is a script, not a module: everything happens at import time. It is
executed here from its own file into a throwaway module namespace, with httpx
and the environment replaced, so importing it never calls Telegram.
"""

import importlib.util
import os
import stat
from pathlib import Path

import dotenv
import httpx
import pytest

REPO = Path(__file__).parent


def _load_script(name: str, path: Path):
    """Execute a script file into a throwaway module and hand it back."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
#  chat_id.py                                                                  #
# --------------------------------------------------------------------------- #
def _exec_chat_id(monkeypatch, payload, token="token"):
    """Run chat_id.py top to bottom with a canned getUpdates answer."""

    class FakeResponse:
        def json(self):
            return payload

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: FakeResponse())
    if token is None:
        monkeypatch.delenv("BOT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BOT_TOKEN", token)

    return _load_script("chat_id_under_test", REPO / "chat_id.py")


def test_chat_id_exits_without_token(monkeypatch):
    with pytest.raises(SystemExit) as exit_info:
        _exec_chat_id(monkeypatch, payload={}, token=None)

    assert "Missing BOT_TOKEN" in str(exit_info.value)


def test_chat_id_exits_on_api_error(monkeypatch):
    with pytest.raises(SystemExit) as exit_info:
        _exec_chat_id(monkeypatch, payload={"ok": False, "description": "Unauthorized"})

    assert "Telegram API error" in str(exit_info.value)


def test_chat_id_reports_nothing_to_show(monkeypatch, capsys):
    # An update carrying neither a message nor a channel post is skipped.
    _exec_chat_id(monkeypatch, payload={"ok": True, "result": [{"edited_message": {}}]})

    out = capsys.readouterr().out
    assert "No updates yet" in out


def test_chat_id_lists_every_kind_of_chat(monkeypatch, capsys):
    payload = {
        "ok": True,
        "result": [
            {
                "message": {
                    "chat": {
                        "id": 1,
                        "type": "private",
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                    }
                }
            },
            {"channel_post": {"chat": {"id": -100, "type": "channel", "title": "Alerts"}}},
            {"message": {"chat": {"id": 2, "type": "group", "username": "squad"}}},
        ],
    }

    _exec_chat_id(monkeypatch, payload=payload)

    out = capsys.readouterr().out
    assert "TARGET_CHAT_ID candidates:" in out
    assert "private: Ada Lovelace" in out
    assert "channel: Alerts" in out
    assert "group: squad" in out


# --------------------------------------------------------------------------- #
#  setup_env.py                                                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """Import setup_env with its .env pointed at a temporary directory."""
    monkeypatch.chdir(tmp_path)
    return _load_script("setup_env_under_test", REPO / "setup_env.py")


def test_load_existing_without_file(setup_env):
    assert setup_env.load_existing() == {}


def test_load_existing_skips_comments_and_junk(setup_env, tmp_path):
    (tmp_path / ".env").write_text(
        "# a comment\n\nTG_API_ID=123\nnot-a-pair\nBOT_TOKEN = spaced \n",
        encoding="utf-8",
    )

    assert setup_env.load_existing() == {"TG_API_ID": "123", "BOT_TOKEN": "spaced"}


def test_main_writes_every_answer(setup_env, tmp_path, monkeypatch, capsys):
    plain = iter(["12345", "777", "custom_source"])
    secrets = iter(["hash", "token"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(plain))
    monkeypatch.setattr(setup_env, "getpass", lambda prompt: next(secrets))

    setup_env.main()

    written = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text().strip().splitlines()
    )
    assert written["TG_API_ID"] == "12345"
    assert written["TG_API_HASH"] == "hash"
    assert written["BOT_TOKEN"] == "token"
    assert written["TARGET_CHAT_ID"] == "777"
    assert written["SOURCE_CHAT"] == "custom_source"
    assert written["SESSION_NAME"] == "session/lexx_relay"

    mode = stat.S_IMODE(os.stat(tmp_path / ".env").st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR
    assert "wrote .env (0600)" in capsys.readouterr().out


def test_main_keeps_existing_values_on_blank_input(setup_env, tmp_path, monkeypatch, capsys):
    # A secret that is already set must be shown as "already set", never echoed.
    (tmp_path / ".env").write_text("TG_API_ID=999\nTG_API_HASH=old-hash\n", encoding="utf-8")
    prompts: list[str] = []

    def record(prompt):
        prompts.append(prompt)
        return "  "

    monkeypatch.setattr("builtins.input", record)
    monkeypatch.setattr(setup_env, "getpass", record)

    setup_env.main()

    written = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text().strip().splitlines()
    )
    assert written["TG_API_ID"] == "999"
    assert written["TG_API_HASH"] == "old-hash"
    assert written["SOURCE_CHAT"] == "source_bot"  # default filled in
    assert written["BOT_TOKEN"] == ""

    assert any("[999]" in p for p in prompts)
    assert any("[already set]" in p for p in prompts)
    assert not any("old-hash" in p for p in prompts)
    assert any("[empty]" in p for p in prompts)


def test_main_reports_nothing_filled(setup_env, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(setup_env, "getpass", lambda prompt: "")

    setup_env.main()

    assert "Filled: SOURCE_CHAT" in capsys.readouterr().out
