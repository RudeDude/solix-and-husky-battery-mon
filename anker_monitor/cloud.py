"""Read F3800 status from the Anker cloud.

The station publishes the same status tags over cloud MQTT once it is on
Wi-Fi. The account password is read from the environment or from a local
.env file and is never printed. Output switches are not sent. A status
request and, if that brings no charge value, one realtime publish request
are the only commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass

from anker_monitor.reading import StationReading, format_reading, reading_from_fields
from device_env import load_env

_F3800_MODELS = {"A1790", "A1790P"}
_CHARGING = {0: "inactive", 1: "solar", 2: "ac", 3: "both"}
_SWITCH = {0: "off", 1: "on"}


class CloudError(RuntimeError):
    """The account could not be read, or the station published no status."""


@dataclass
class CloudResult:
    name: str
    serial_number: str
    model: str
    wifi_online: bool | None
    reading: StationReading | None
    detail: str = ""


def load_credentials() -> tuple[str, str, str]:
    """Return the Anker email, password, and country code.

    A local .env file is loaded when it exists. A terminal prompt is used
    only for values that are still missing.
    """

    load_env()
    email = os.environ.get("ANKERUSER", "").strip()
    password = os.environ.get("ANKERPASSWORD", "")
    country = os.environ.get("ANKERCOUNTRY", "").strip()
    if sys.stdin.isatty():
        if not email:
            email = input("Anker account email: ").strip()
        if not password:
            password = getpass.getpass("Anker account password: ")
        if not country:
            country = input("Anker country code (for example US): ").strip()
    return email, password, country


def _credential_message() -> str:
    return (
        "Cloud login needs ANKERUSER, ANKERPASSWORD, and ANKERCOUNTRY. "
        "Copy .env.example to .env in this folder and fill those in. "
        "The password stays in that file and is not printed."
    )


def _redact(text: str, password: str) -> str:
    if password:
        return text.replace(password, "****")
    return text


def _online(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "online", "Online"):
        return True
    if value in (0, "0", "false", "False", "offline", "Offline"):
        return False
    return None


def _switch(value: object) -> object:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value in _SWITCH:
        return _SWITCH[value]
    return value


def _charging(value: object) -> object:
    if value in _CHARGING:
        return _CHARGING[value]
    return value


def fields_from_mqtt(status: dict[str, object]) -> dict[str, object]:
    """Map published A1790 MQTT names onto the local reading fields."""

    discharge = status.get("bat_discharge_power")
    if discharge is None:
        discharge = status.get("bat_discharge_power?")
    return {
        "soc_percent": status.get("battery_soc"),
        "soc_other_percent": status.get("main_battery_soc"),
        "temperature_c": status.get("temperature"),
        "ac_power_in_w": status.get("ac_input_power"),
        "ac_power_out_w": status.get("ac_output_power"),
        "solar_power_w": status.get("photovoltaic_power"),
        "solar_pv1_w": status.get("pv_1_power"),
        "solar_pv2_w": status.get("pv_2_power"),
        "charge_power_w": status.get("bat_charge_power"),
        "output_power_w": status.get("output_power_total"),
        "discharge_power_w": discharge,
        "usb_c1_w": status.get("usbc_1_power"),
        "usb_c2_w": status.get("usbc_2_power"),
        "usb_c3_w": status.get("usbc_3_power"),
        "usb_a1_w": status.get("usba_1_power"),
        "usb_a2_w": status.get("usba_2_power"),
        "ac_output": _switch(status.get("ac_output_power_switch")),
        "charging": _charging(status.get("charging_status")),
        "hours_remaining": status.get("remaining_time_hours"),
        "serial_number": status.get("device_sn"),
    }


def _model(device: dict) -> str:
    return str(device.get("device_pn") or device.get("product_code") or "")


def select_station(devices: dict, serial: str = "") -> dict | None:
    """Return the F3800 record, optionally limited to one serial."""

    wanted = serial.strip()
    matches = []
    for device in devices.values():
        if not isinstance(device, dict):
            continue
        if _model(device) not in _F3800_MODELS:
            continue
        device_serial = str(device.get("device_sn") or "")
        if wanted and device_serial != wanted:
            continue
        matches.append(device)
    if not matches:
        return None
    online = [device for device in matches if _online(device.get("wifi_online")) is True]
    return (online or matches)[0]


def _missing_station(devices: dict) -> str:
    listed = []
    for device in devices.values():
        if not isinstance(device, dict):
            continue
        model = _model(device) or "unknown"
        alias = device.get("alias") or device.get("device_name") or ""
        listed.append(f"{model} {alias}".strip())
    if not listed:
        found = "The account lists no devices."
    else:
        found = "The account lists: " + ", ".join(listed) + "."
    return (
        "No F3800 (A1790 or A1790P) is on this Anker account. "
        "Bind the station in the Anker app first. " + found
    )


def _no_status_detail(wifi_online: bool | None) -> str:
    if wifi_online is False:
        return (
            "The account lists this F3800, and its Wi-Fi is offline. "
            "The cloud receives charge and power only after the station joins "
            "Wi-Fi in the Anker app. Bluetooth and Wi-Fi are not used at the "
            "same time, so the Bluetooth advertisement stops once Wi-Fi is up. "
            "This LAN has no F3800 register map, so the station IP is not a data source."
        )
    return (
        "The account login worked, and the station published no status frame. "
        "Confirm the station is awake and on Wi-Fi. A Home Power Panel keeps "
        "the F3800 off its own Wi-Fi and publishes through the panel."
    )


def _merged_status(station: object) -> dict:
    status = dict(getattr(station, "get_status")() or {})
    api = getattr(station, "api", None)
    devices = getattr(api, "devices", {}) if api is not None else {}
    record = devices.get(getattr(station, "sn", "")) or {}
    extra = record.get("mqtt_data") or {}
    if isinstance(extra, dict):
        status.update(extra)
    return status


async def _wait_for_charge(station: object, seconds: float) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    status = _merged_status(station)
    while status.get("battery_soc") is None and loop.time() < deadline:
        await asyncio.sleep(0.5)
        status = _merged_status(station)
    return status


async def read_cloud(*, wait: float = 25.0, serial: str = "") -> CloudResult:
    """Log in, request one status publication, and return the decoded frame."""

    email, password, country = load_credentials()
    if not email or not password or not country:
        raise CloudError(_credential_message())
    try:
        from aiohttp import ClientSession
        from anker_solix_api.api import AnkerSolixApi
        from anker_solix_api.mqtt_factory import SolixMqttDeviceFactory
        from anker_solix_api.mqtt_pps import SolixMqttDevicePps
    except ImportError as exc:
        raise CloudError(
            "anker-solix-api is not installed. Run pip install -r requirements.txt."
        ) from exc

    logger = logging.getLogger("anker_solix_api")
    logger.setLevel(logging.WARNING)
    async with ClientSession() as websession:
        api = AnkerSolixApi(email, password, country, websession, logger)
        try:
            await api.update_sites()
            await api.update_device_details()
        except Exception as exc:
            raise CloudError(f"Anker login failed: {_redact(str(exc), password)}") from exc
        record = select_station(getattr(api, "devices", {}) or {}, serial)
        if record is None:
            raise CloudError(_missing_station(getattr(api, "devices", {}) or {}))
        name = str(record.get("alias") or record.get("device_name") or "F3800")
        device_serial = str(record.get("device_sn") or "")
        model = _model(record)
        wifi_online = _online(record.get("wifi_online"))
        session = None
        poller: asyncio.Task | None = None
        status: dict = {}
        try:
            session = await api.startMqttSession()
            if session is None or not session.is_connected():
                raise CloudError("the Anker MQTT server did not accept the account session")
            station = SolixMqttDeviceFactory(api, device_serial).create_device()
            if station is None:
                station = SolixMqttDevicePps(api, device_serial)
            topics: set[str] = set()
            prefix = session.get_topic_prefix(deviceDict=record)
            if prefix:
                topics.add(f"{prefix}#")
            command_prefix = session.get_topic_prefix(deviceDict=record, publish=True)
            if command_prefix:
                topics.add(f"{command_prefix}#")
            if not topics:
                raise CloudError("the account session has no MQTT topic for this station")
            poller = asyncio.create_task(
                session.message_poller(
                    topics=topics,
                    trigger_devices=set(),
                    timeout=max(int(wait), 1),
                )
            )
            await asyncio.sleep(2)
            session.status_request(deviceDict=record, wait_for_publish=2)
            status = await _wait_for_charge(station, 8)
            if status.get("battery_soc") is None:
                session.realtime_trigger(
                    deviceDict=record,
                    timeout=60,
                    wait_for_publish=2,
                )
                status = await _wait_for_charge(station, max(wait - 10, 5))
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(
                f"Anker cloud request failed: {_redact(str(exc), password)}"
            ) from exc
        finally:
            if poller is not None:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await poller
            if session is not None:
                with contextlib.suppress(Exception):
                    session.cleanup()
        reading = None
        detail = ""
        if status.get("battery_soc") is None and status.get("main_battery_soc") is None:
            detail = _no_status_detail(wifi_online)
        else:
            reading = reading_from_fields(fields_from_mqtt(status), source="solix-mqtt")
            if not reading.serial_number:
                reading.serial_number = device_serial or None
        return CloudResult(
            name=name,
            serial_number=device_serial,
            model=model,
            wifi_online=wifi_online,
            reading=reading,
            detail=detail,
        )


def format_cloud(result: CloudResult) -> str:
    wifi = "unknown" if result.wifi_online is None else "online" if result.wifi_online else "offline"
    lines = [
        f"{result.name}  {result.model}  {result.serial_number}",
        f"wifi: {wifi}",
    ]
    if result.reading is not None:
        lines.append(format_reading(result.reading))
    elif result.detail:
        lines.append(result.detail)
    return "\n".join(lines)


def cloud_to_json(result: CloudResult) -> str:
    payload = {
        "name": result.name,
        "serial_number": result.serial_number,
        "model": result.model,
        "wifi_online": result.wifi_online,
        "reading": None if result.reading is None else asdict(result.reading),
        "detail": result.detail,
    }
    return json.dumps(payload, indent=2)
