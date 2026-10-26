"""Runtime identities come from the environment.

Parser fixtures in the other tests keep their own address literals. These
checks set the variables themselves and put the previous values back.
"""

from __future__ import annotations

import os
import unittest

from anker_monitor.__main__ import _parser as anker_parser
from anker_monitor.advert import advertisement_matches
from collector.__main__ import _parser as collector_parser
from device_env import setting
from husky_monitor.__main__ import _parser as husky_parser
from husky_monitor.client import husky_name

_KEYS = (
    "HUSKY_BT_NAME",
    "HUSKY_BT_ADDRESS",
    "ANKER_BT_NAME",
    "ANKER_BT_ADDRESS",
    "ANKER_BT_IDENTITY",
    "ANKER_HOST",
    "ANKER_SERIAL",
)


class DeviceEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _KEYS}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_blank_setting_stays_blank(self) -> None:
        os.environ.pop("HUSKY_BT_NAME", None)
        self.assertEqual(husky_name(), "")
        self.assertEqual(setting("HUSKY_BT_NAME"), "")

    def test_cli_defaults_follow_the_environment(self) -> None:
        os.environ["HUSKY_BT_NAME"] = "RADIO"
        os.environ["HUSKY_BT_ADDRESS"] = "01:02:03:04:05:06"
        os.environ["ANKER_BT_NAME"] = "STATION"
        os.environ["ANKER_BT_ADDRESS"] = "0A:0B:0C:0D:0E:0F"
        os.environ["ANKER_BT_IDENTITY"] = "0A:0B:0C:0D:0E:10"
        os.environ["ANKER_HOST"] = "192.0.2.10"
        os.environ["ANKER_SERIAL"] = "SN1"

        husky = husky_parser().parse_args(["scan"])
        self.assertEqual(husky.name, "RADIO")
        self.assertEqual(husky.address, "01:02:03:04:05:06")

        anker = anker_parser().parse_args(["cloud"])
        self.assertEqual(anker.name, "STATION")
        self.assertEqual(anker.address, "0A:0B:0C:0D:0E:0F")
        self.assertEqual(anker.host, "192.0.2.10")
        self.assertEqual(anker.serial, "SN1")

        collected = collector_parser().parse_args([])
        self.assertEqual(collected.husky_name, "RADIO")
        self.assertEqual(collected.husky_address, "01:02:03:04:05:06")
        self.assertEqual(collected.anker_name, "STATION")
        self.assertEqual(collected.anker_address, "0A:0B:0C:0D:0E:0F")
        self.assertEqual(collected.anker_host, "192.0.2.10")

    def test_configured_identity_is_accepted_with_the_label(self) -> None:
        os.environ["ANKER_BT_ADDRESS"] = "11:22:33:44:55:66"
        os.environ["ANKER_BT_IDENTITY"] = "AA:BB:CC:DD:EE:01"
        self.assertTrue(
            advertisement_matches(
                name="Other",
                address="AA:BB:CC:DD:EE:01",
                manufacturer=None,
                wanted_name="",
                wanted_address="11:22:33:44:55:66",
            )
        )
        self.assertFalse(
            advertisement_matches(
                name="Other",
                address="AA:BB:CC:DD:EE:01",
                manufacturer=None,
                wanted_name="",
                wanted_address="10:00:00:00:00:01",
            )
        )


if __name__ == "__main__":
    unittest.main()
