import asyncio
import argparse
import os

from dotenv import load_dotenv

load_dotenv(override=True)

import logfire

from debug_auth import openrouter_api_key_parts
from machine import StateMachine
from states import (
    create_human_update_state,
    create_identity_update_state,
    create_journaling_state,
    create_standard_state,
)
from telegram_io import TelegramIO

logfire.configure()
logfire.instrument_pydantic_ai()

def _parse_int_list(raw: str) -> list[int]:
    items: list[int] = []
    for part in (raw or "").split(","):
        p = part.strip()
        if not p:
            continue
        items.append(int(p))
    return items


def main():
    parser = argparse.ArgumentParser(description="Run clankops state machine")
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Run in Telegram mode (messages are read/sent via Telegram bot API).",
    )
    parser.add_argument(
        "--dangerzone",
        action="store_true",
        help="Enable dangerous host command execution tool in standard mode.",
    )
    args = parser.parse_args()

    raw_api_key, api_key = openrouter_api_key_parts()
    if raw_api_key and raw_api_key != api_key:
        print(
            "Warning: OPENROUTER_API_KEY had leading/trailing whitespace. "
            "Using trimmed value."
        )
        os.environ["OPENROUTER_API_KEY"] = api_key

    if not api_key:
        print("Error: OPENROUTER_API_KEY not set.")
        print("Set it in your environment or create a .env file (see .env.example).")
        return

        telegram_io = None
        main_user_id = 0
    if args.telegram:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_user_id_raw = os.environ.get("TELEGRAM_USER_ID", "").strip()
        extra_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()

        if not bot_token:
            print("Error: TELEGRAM_BOT_TOKEN is required for --telegram mode.")
            return
        if not allowed_user_id_raw:
            print("Error: TELEGRAM_USER_ID is required for --telegram mode.")
            return

        try:
            main_user_id = int(allowed_user_id_raw)
        except ValueError:
            print("Error: TELEGRAM_USER_ID must be an integer Telegram user ID.")
            return

        allowed_user_ids: set[int] = {int(main_user_id)}
        try:
            for uid in _parse_int_list(extra_raw):
                allowed_user_ids.add(int(uid))
        except Exception:
            print("Error: TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers.")
            return

        telegram_io = TelegramIO(bot_token=bot_token, allowed_user_ids=allowed_user_ids)
        try:
            me = telegram_io.get_me()
            username = str(me.get("username", "")).strip()
            label = f"@{username}" if username else "bot"
            print(
                f"[telegram] Connected as {label}. "
                f"Main user ID: {main_user_id}. "
                f"Allowed user IDs: {', '.join(str(x) for x in sorted(allowed_user_ids))}."
            )
        except Exception as err:
            print(f"Error: could not initialize Telegram bot: {err}")
            return

    machine = StateMachine(
        initial_state="standard",
        input_reader=telegram_io.read_message if telegram_io else None,
        response_sink=telegram_io.send_message if telegram_io else None,
        main_user_id=int(main_user_id or 0),
    )
    machine.add_state(create_standard_state(enable_dangerzone=args.dangerzone))
    machine.add_state(create_journaling_state())
    machine.add_state(create_identity_update_state())
    machine.add_state(create_human_update_state())

    try:
        asyncio.run(machine.run())
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
