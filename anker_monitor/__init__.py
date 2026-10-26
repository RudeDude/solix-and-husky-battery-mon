"""Read an Anker SOLIX F3800.

Bluetooth uses ``ANKER_BT_NAME`` and ``ANKER_BT_ADDRESS`` from ``.env``.
Charge, power, and temperature come from the status frame while the station
is advertising, or from the Anker cloud once it is on Wi-Fi. This package
does not switch outputs.
"""

__version__ = "0.2.0"
