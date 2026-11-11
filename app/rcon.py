"""Minimal client for Rust's WebSocket RCON (rcon.web 1)."""
from __future__ import annotations

import asyncio
import json
import os
import random
from urllib.parse import quote

import websockets

HOST = os.environ.get("RCON_HOST", "")
PORT = os.environ.get("RCON_PORT", "28016")
PASSWORD = os.environ.get("RCON_PASSWORD", "")


class RconError(Exception):
    pass


def configured() -> bool:
    return bool(HOST and PASSWORD)


async def command(text: str, timeout: float = 8.0) -> str:
    """Run one console command and return the server's reply."""
    if not configured():
        raise RconError("RCON is not configured.")
    ident = random.randint(1000, 999_999)
    url = f"ws://{HOST}:{PORT}/{quote(PASSWORD, safe='')}"
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(url, open_timeout=5) as ws:
                await ws.send(json.dumps({"Identifier": ident, "Message": text, "Name": "Nitrous"}))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("Identifier") == ident:
                        return str(msg.get("Message", ""))
        raise RconError("The server closed the connection.")
    except TimeoutError:
        raise RconError("The server did not answer in time.") from None
    except (OSError, websockets.WebSocketException) as exc:
        # Never include the URL here: it contains the password.
        raise RconError(f"Could not reach the server's RCON ({type(exc).__name__}).") from None
