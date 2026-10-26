"""Load this installation's device identities from the repo-root .env file.

MAC addresses, Bluetooth addresses, the station IP, and the station serial
are configuration. Protocol constants stay in the monitor modules. The Anker
account password is read by the cloud client and is never printed here.
"""

from __future__ import annotations

import os
from pathlib import Path

_loaded = False


def load_env() -> None:
    """Load the repo-root .env file once. Existing variables are left as they are."""

    global _loaded
    if _loaded:
        return
    _loaded = True
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_file)


def setting(name: str) -> str:
    """Return one trimmed configuration value, or an empty string."""

    load_env()
    return os.environ.get(name, "").strip()
