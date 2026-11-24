"""Bridges the USB-connected Taiwan phone and a Telegram bot.

Forwards received SMS and incoming-call alerts to the owner's chat, and sends the owner's
Telegram replies to those messages back out as SMS to the same number.
"""

import base64
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ADB = "/opt/homebrew/bin/adb"
HERE = Path(__file__).parent
TRANSLATE = HERE / "translate"
BOT_ENV = HERE / ".env"
STATE = HERE / "state.json"
POLL_SECONDS = 3
DISCONNECT_ALERT_SECONDS = 300
REPLY_MAP_LIMIT = 1000


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


def load_bot():
    env = dict(
        line.split("=", 1)
        for line in BOT_ENV.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return env["TELEGRAM_BOT_TOKEN"].strip(), int(env["TELEGRAM_CHAT_ID"].strip()), env.get("CALLS_URL", "").strip()


def telegram(bot, method, **params):
    token = bot[0]
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    with urllib.request.urlopen(url, data, timeout=20) as r:
        body = json.load(r)
    if body.get("ok") is not True:
        raise RuntimeError(f"telegram {method} failed: {body}")
    return body["result"]


def send(bot, text, reply_to=None):
    """Sends text to the owner and returns the Telegram message id."""
    return telegram(bot, "sendMessage", chat_id=bot[1], text=text, reply_to_message_id=reply_to)[
        "message_id"
    ]


def adb(*args):
    r = subprocess.run([ADB, "shell", *args], capture_output=True, text=True, timeout=90)
    out = r.stdout + r.stderr
    if r.returncode != 0 or "Exception" in out:
        raise RuntimeError(f"adb {' '.join(args[:3])}: {out.strip()}")
    return r.stdout


def messages_after(last_id):
    out = adb("content", "query", "--uri", f"'content://smsbridge/sms?after={last_id}'")
    if out.strip() == "No result found.":
        return []
    rows = [line.split(" j=", 1)[1] for line in out.splitlines() if " j=" in line]
    return [json.loads(base64.b64decode(r)) for r in rows]


def translate(text):
    """English translation from the on-device translate binary; empty when already English."""
    r = subprocess.run([TRANSLATE], input=text, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"translate failed: {r.stderr.strip()}")
    return r.stdout.strip()


def send_sms(number, body):
    b64 = base64.b64encode(body.encode()).decode()
    adb("content", "call", "--uri", "content://smsbridge", "--method", "send",
        "--arg", number, "--extra", f"b:s:{b64}")


def ringing_number():
    """Returns the caller's number while the phone rings, else None."""
    out = adb("dumpsys", "telephony.registry")
    state = number = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("mCallState=") and state is None:
            state = line.split("=", 1)[1]
        elif line.startswith("mCallIncomingNumber=") and number is None:
            number = line.split("=", 1)[1]
    return (number or "unknown number") if state == "1" else None


class State:
    """last_id: newest forwarded SMS. offset: next Telegram update. numbers: message id -> phone."""

    def __init__(self):
        data = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.last_id = data.get("last_id")
        self.offset = data.get("offset")
        self.numbers = data.get("numbers", {})

    def remember(self, message_id, number):
        self.numbers[str(message_id)] = number
        for key in list(self.numbers)[:-REPLY_MAP_LIMIT]:
            del self.numbers[key]

    def save(self):
        STATE.write_text(
            json.dumps({"last_id": self.last_id, "offset": self.offset, "numbers": self.numbers})
        )


def forward_new_sms(bot, state):
    if state.last_id is None:
        msgs = messages_after(0)
        state.last_id = msgs[-1]["id"] if msgs else 0
        state.save()
        log(f"initialized last_id={state.last_id}")
    for m in messages_after(state.last_id):
        when = datetime.fromtimestamp(m["date"] / 1000).strftime("%b %d %H:%M")
        try:
            english = translate(m["body"])
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"sms {m['id']}: {e}")
            english = f"(translation failed: {e})"
        text = f"TW text from {m['address']} ({when}):\n{m['body']}"
        if english:
            text += f"\n\nEnglish:\n{english}"
        mid = send(bot, text)
        state.last_id = m["id"]
        state.remember(mid, m["address"])
        state.save()
        log(f"forwarded sms {state.last_id}")


def handle_replies(bot, state):
    for update in telegram(bot, "getUpdates", offset=state.offset, timeout=0):
        state.offset = update["update_id"] + 1
        state.save()
        msg = update.get("message")
        if not msg or msg["chat"]["id"] != bot[1]:
            log(f"ignored update {update['update_id']}")
            continue
        replied = msg.get("reply_to_message")
        number = state.numbers.get(str(replied["message_id"])) if replied else None
        if not number or not msg.get("text"):
            send(bot, "Swipe left on a forwarded text or call alert to reply to that number.",
                 msg["message_id"])
            continue
        if not re.fullmatch(r"\+?[0-9]{3,15}", number):
            send(bot, f"Can't text {number}: not a phone number.", msg["message_id"])
            continue
        try:
            send_sms(number, msg["text"])
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"sms send to {number} failed: {e}")
            send(bot, f"Not sent to {number}: {str(e)[:300]}", msg["message_id"])
            continue
        log(f"sent sms to {number}")
        send(bot, f"Sent to {number}.", msg["message_id"])


def main():
    bot = load_bot()
    state = State()
    was_ringing = False
    down_since = None
    alerted_down = False
    log(f"started, last_id={state.last_id}")
    while True:
        try:
            forward_new_sms(bot, state)
            caller = ringing_number()
            if caller and not was_ringing:
                answer = f"\nAnswer: {bot[2]}" if bot[2] else ""
                state.remember(send(bot, f"TW phone ringing: call from {caller}{answer}"), caller)
                state.save()
                log("forwarded call alert")
            was_ringing = caller is not None
            handle_replies(bot, state)
            if alerted_down:
                send(bot, "TW phone reconnected to the Mac mini.")
            down_since = None
            alerted_down = False
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            log(f"error: {e}")
            down_since = down_since or time.monotonic()
            if not alerted_down and time.monotonic() - down_since > DISCONNECT_ALERT_SECONDS:
                try:
                    send(bot, f"TW phone bridge failing for 5+ minutes: {e}")
                    alerted_down = True
                except OSError as send_err:
                    log(f"disconnect alert failed: {send_err}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
