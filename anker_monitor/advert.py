"""Match an F3800 advertisement.

Anker puts a short identity record in manufacturer data under company id
``0xFFFF``. On this F3800 the record is eight bytes: a version, the six-byte
identity MAC, and a bind byte. The BlueZ address is a separate random
address and is not that MAC.
"""

from __future__ import annotations

from dataclasses import dataclass

from device_env import setting


COMPANY_ID = 0xFFFF


def anker_name() -> str:
    """Advertised Bluetooth name (``ANKER_BT_NAME``)."""

    return setting("ANKER_BT_NAME")


def anker_address() -> str:
    """Label MAC printed on the station (``ANKER_BT_ADDRESS``)."""

    return setting("ANKER_BT_ADDRESS")


def anker_identity() -> str:
    """Identity MAC from the advertisement (``ANKER_BT_IDENTITY``)."""

    return setting("ANKER_BT_IDENTITY")


def anker_host() -> str:
    """LAN address to probe (``ANKER_HOST``)."""

    return setting("ANKER_HOST")


def anker_serial() -> str:
    """Station serial (``ANKER_SERIAL``). Empty selects the A1790 on the account."""

    return setting("ANKER_SERIAL")


class AdvertError(ValueError):
    """A Bluetooth address is not twelve hex digits."""


@dataclass(frozen=True)
class AdvertIdentity:
    version: int
    mac: str
    bind_type: int


def normalize_address(address: str) -> str:
    digits = "".join(char for char in address if char.isalnum())
    if len(digits) != 12 or any(char not in "0123456789abcdefABCDEF" for char in digits):
        raise AdvertError(f"{address!r} is not a Bluetooth address")
    digits = digits.upper()
    return ":".join(digits[index : index + 2] for index in range(0, 12, 2))


def address_aliases(address: str) -> set[str]:
    """Identity addresses that belong with one configured MAC.

    The advertisement often carries the label MAC with the last octet
    increased by one. Both values identify the same radio.
    """

    normalized = normalize_address(address)
    parts = normalized.split(":")
    last = (int(parts[-1], 16) + 1) & 0xFF
    parts[-1] = f"{last:02X}"
    return {normalized, ":".join(parts)}


def _normalized_setting(name: str) -> str:
    raw = setting(name)
    if not raw:
        return ""
    try:
        return normalize_address(raw)
    except AdvertError:
        return ""


def accepted_addresses(wanted_address: str) -> set[str]:
    """Addresses that count as the station the caller asked for.

    A configured label also accepts its last-octet-plus-one alias. When the
    caller asked for that label, ``ANKER_BT_IDENTITY`` is accepted as well,
    including when it is not one octet higher.
    """

    aliases: set[str] = set()
    if wanted_address:
        try:
            aliases = address_aliases(wanted_address)
        except AdvertError:
            return set()
    identity = _normalized_setting("ANKER_BT_IDENTITY")
    label = _normalized_setting("ANKER_BT_ADDRESS")
    if not identity:
        return aliases
    if not wanted_address:
        if not label:
            aliases.add(identity)
        return aliases
    try:
        wanted = normalize_address(wanted_address)
    except AdvertError:
        return aliases
    if wanted == label or wanted == identity:
        aliases.add(identity)
    return aliases


def parse_manufacturer(data: bytes) -> AdvertIdentity | None:
    """Read the identity record, or return None when the bytes are not one."""

    if len(data) < 8:
        return None
    version = data[0]
    if version not in (1, 2):
        return None
    mac = ":".join(f"{byte:02X}" for byte in data[1:7])
    return AdvertIdentity(version=version, mac=mac, bind_type=data[7])


def advertisement_matches(
    *,
    name: str,
    address: str,
    manufacturer: bytes | None,
    wanted_name: str,
    wanted_address: str,
) -> bool:
    """True when the advertisement is the configured station."""

    if wanted_name and name and name.casefold().strip() == wanted_name.casefold().strip():
        return True
    aliases = accepted_addresses(wanted_address)
    if address and aliases:
        try:
            if normalize_address(address) in aliases:
                return True
        except AdvertError:
            pass
    if manufacturer and aliases:
        identity = parse_manufacturer(manufacturer)
        if identity is not None and identity.mac in aliases:
            return True
    return False
