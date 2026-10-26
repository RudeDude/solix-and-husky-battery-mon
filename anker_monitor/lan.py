"""See whether the station answers on the LAN.

Anker's official local API is Modbus TCP on port 502. The published device
list is Solarbank, XE, smart plug, and smart meter models. The F3800 is not
in that list, so an open port is reported and not read.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass


MODBUS_PORT = 502


@dataclass(frozen=True)
class LanProbe:
    host: str
    reachable: bool
    modbus_open: bool
    detail: str


def describe_lan(host: str, *, reachable: bool, modbus_open: bool) -> str:
    if modbus_open:
        return (
            f"{host}: port {MODBUS_PORT} is open. The official Anker Modbus "
            "integration has no F3800 register map, so registers are left unread."
        )
    if reachable:
        return (
            f"{host}: the host answers, and port {MODBUS_PORT} is closed. "
            "The official Anker Modbus integration has no F3800 register map."
        )
    return (
        f"{host}: no answer on this LAN. The official Anker Modbus integration "
        "has no F3800 register map."
    )


def probe_lan(host: str, timeout: float = 2.0) -> LanProbe:
    """Try one TCP connection to the Modbus port."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    reachable = False
    modbus_open = False
    try:
        error = sock.connect_ex((host, MODBUS_PORT))
        if error == 0:
            reachable = True
            modbus_open = True
        elif error == 111:
            reachable = True
    except OSError:
        reachable = False
    finally:
        sock.close()
    return LanProbe(
        host=host,
        reachable=reachable,
        modbus_open=modbus_open,
        detail=describe_lan(host, reachable=reachable, modbus_open=modbus_open),
    )
