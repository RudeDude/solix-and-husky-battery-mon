# Battery monitors

Read-only Python clients for two batteries.

- [Husky 2](#husky-2-bluetooth-monitor) over Bluetooth.
  [Sources](#sources-examined-for-the-husky).
- [Anker SOLIX F3800](#anker-solix-f3800) over Bluetooth while the IoT icon is showing, or over the Anker cloud once the station is on Wi-Fi. The LAN address is probed and is not a data source.
  [Sources](#sources-examined-for-the-f3800).
- [Both batteries](#collect-both) sampled together into one NDJSON file.
- [Capacity and runtime](#capacity-and-runtime) from those logs.

The Husky is the external pack on the F3800.
[How the Husky is attached](#how-the-husky-is-attached).

Names, MAC addresses, the F3800 LAN address, and the F3800 serial are read
from `.env`. Copy `.env.example` to `.env` and fill in the placeholders.
`ANKERPASSWORD` stays in that file and is not printed.

# How the Husky is attached

The Husky is the external pack on the F3800 expansion battery port. With the
station powered on, that port is the internal 51.2 V bus, and other owners
parallel a 48 V LiFePO4 pack there so the station's inverter and chargers
serve both. Anker's BP3800 uses the same port and a center data pin so the
station can count the extra pack. This link is
[NoiseLab47's printed plug](https://www.thingiverse.com/thing:7003775)
on the power pins, with that data pin left unused. The 16-cell Husky shares
the station's bus, and the percentage the F3800 reports is still its own
pack. Some owners feed a 48 V pack into an XT60 solar input instead. That
path only charges the station.

# Husky 2 Bluetooth monitor

Read-only Python client for the Bluetooth radio in a BigBattery 48 V Husky 2
(`FHSKY-48051-G2`). The radio name and MAC come from `HUSKY_BT_NAME` and
`HUSKY_BT_ADDRESS`. It prints pack voltage, current, state of charge,
temperatures, and each cell voltage.

The phone app for this battery is [Battery Monitor BL](https://play.google.com/store/apps/details?id=com.ztpower.monitor.bl) by Zetarapower ([App Store](https://apps.apple.com/us/app/batterymonitorbl/id1585654219)). This
program does not change module id, CAN protocol, RS485 protocol, or any
protection setting.

## What the radio actually speaks

Nothing public names `BB-GOLF-BT1128`. A live GATT listing from this pack is:

- service `1006`: notify `1007`, write `1008`
- service `fa00`: write `fa01` (left unused; it is not the data channel)

That profile, and the Telink address prefix `A4:C1:37`, match the Zeta /
Voltgo LiFePO4 batteries documented in
[lumberbarons/voltgo `PROTOCOL.md`](https://github.com/lumberbarons/voltgo/blob/main/PROTOCOL.md).
The status poll is one Modbus read of 41 holding registers:

```text
01 03 00 00 00 29 84 14
```

Register 0 is pack voltage (0.01 V), register 1 is current (0.1 A, positive
means charging), registers 2–17 are up to 16 cell voltages in millivolts, and
registers 18–20 are three temperatures in °C. On 8S Zeta packs, state of
charge is register 21. This Husky leaves that register at zero and reports
the percentage in register 24. The capacity register in the 8S map reads 0
here, so capacity is omitted instead of printed as 0 Ah.

The phone app can also select a module id and a CAN or RS485 protocol. Those
writes are not sent. A Seplos `ff00` radio is still accepted if one shows up
instead. That fallback follows
[aiobmsble `seplos_v2_bms.py`](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/seplos_v2_bms.py)
and the frames in
[syssi/esphome-seplos-bms](https://github.com/syssi/esphome-seplos-bms)
([issue 52](https://github.com/syssi/esphome-seplos-bms/issues/52)).
This Husky does not expose service `ff00`.

## Run

Turn the battery on, disconnect the phone app, and turn the computer radio on
(`bluetoothctl power on`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m husky_monitor scan
.venv/bin/python -m husky_monitor read
.venv/bin/python -m husky_monitor read --watch 5
.venv/bin/python -m husky_monitor read --json
.venv/bin/python -m husky_monitor inspect
```

`--watch 5` connects once and repeats the same read-only status poll every
5 seconds. The radio stays up until Ctrl-C. A `read` without `--watch`
disconnects after one sample. The scan runs only on that first connection,
so later samples are the poll itself.

Positive current means charging. This radio reports three pack temperatures,
not one temperature for every cell, and the output labels them Temperature 1,
2, and 3. An unused probe reads 0 °C. The text output still shows it. The
[combined log](#collect-both) leaves that probe out.

The BMS inside this battery is expected to be 16 cells and about 100 Ah. The
program prints a note when the reply says otherwise.

## Sources examined for the Husky

The status request and register layout come from
[lumberbarons/voltgo](https://github.com/lumberbarons/voltgo):

- [`PROTOCOL.md`](https://github.com/lumberbarons/voltgo/blob/main/PROTOCOL.md)
  documents service `1006`, characteristics `1007` and `1008`, the unused
  `fa00` service, the Telink address prefix, and the 41-register read
  `01 03 00 00 00 29 84 14`.
- [`battery/types.go`](https://github.com/lumberbarons/voltgo/blob/main/battery/types.go)
  names the same holding registers.
- [`internal/protocol/corpus_test.go`](https://github.com/lumberbarons/voltgo/blob/main/internal/protocol/corpus_test.go)
  holds the idle 8S capture used as a parser fixture. The 16S Husky image
  was read from this pack and is not in that repository.

The Seplos path was checked before the GATT listing showed it is the wrong
radio. It remains only as a fallback:

- [patman15/aiobmsble `aiobmsble/bms/seplos_v2_bms.py`](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/seplos_v2_bms.py)
  and its `crc_xmodem` helper in
  [`basebms.py`](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/basebms.py).
  Service `ff00`, notify
  `ff01`, write `ff02`, telemetry `0x61`, manufacturer `0x51`.
- [syssi/esphome-seplos-bms](https://github.com/syssi/esphome-seplos-bms)
  README, including the ASCII query `~20004642E00200FD37`.
- [esphome-seplos-bms issue 52](https://github.com/syssi/esphome-seplos-bms/issues/52)
  for the manufacturer frame `7E 10 00 46 51 00 00 3A 7F 0D`.
- The EMU1101 Bluetooth protocol PDF mirrored by Akkudoktor,
  <https://akkudoktor.net/uploads/short-url/yRjyygvUDE8TAHOcKMXMGSy235H.pdf>,
  which those two projects implement. The open implementations were the
  frames actually coded. This pack does not speak them.

# Anker SOLIX F3800

Read-only client for an Anker SOLIX F3800 (product code A1790). It looks for
`ANKER_BT_NAME`. `ANKER_BT_ADDRESS` is the MAC printed on the station.
`ANKER_BT_IDENTITY` is the MAC inside the advertisement, often that label
with the last octet increased by one. BlueZ connects to a separate random
address, so the program matches the name, the label, and that identity
rather than the random address.

## What is Bluetooth and what is LAN

The Anker app uses Bluetooth while the station is off Wi-Fi. Press the IoT
button until the icon is on the screen, then start `read`. The handshake is
the slow part. `--watch` keeps that session open and prints each status
frame the station sends. Charge, power, and temperature are not in the
Bluetooth advertisement or in a plain GATT value. The radio reported an
ESP32 module, firmware `0.0.0.3`, and the serial in `ANKER_SERIAL`. A session
can finish the handshake before the first frame arrives. The published frame
has no pack-voltage field.

Putting the station on Wi-Fi does not open a local data port for the F3800.
The address in `ANKER_HOST` answers on this LAN, and port 502 is closed. Anker's local Modbus integration,
[anker-charging/ha-anker-solix-official](https://github.com/anker-charging/ha-anker-solix-official),
covers Solarbank, XE, smart plug, and smart meter models. A search of that
repository for `F3800` and `A1790` returned nothing, so port 502 is probed
and registers are left unread. Bluetooth and Wi-Fi are not used at the same
time: [HaSolixBLE](https://github.com/flip-dots/HaSolixBLE) records that
limit, and once the station joins Wi-Fi the `AnkerAdv` advertisement stops.

The station then publishes status to the Anker cloud. The cloud web API for
a standalone power station does not include charge or power
([ha-anker-solix issue 96](https://github.com/thomluther/ha-anker-solix/issues/96)).
Those values arrive as MQTT message `0405` on Anker's server, which is what
the app requests.
[thomluther/anker-solix-api](https://github.com/thomluther/anker-solix-api)
decodes that message. Tag `ad` (`battery_soc`) is the percentage the app
shows. Tag `a6` is AC output watts and tag `b2` is total output watts. Tag
`c0` (`main_battery_soc`) is printed only when it differs
([discussion 241](https://github.com/thomluther/anker-solix-api/discussions/241)).
The published `0405` map has no pack-voltage field, and the same tags are
what Bluetooth message `c840` carries.

`cloud` logs in with `ANKERUSER`, `ANKERPASSWORD`, and `ANKERCOUNTRY` from
the environment or from `.env`. It sends a status request and, if no charge
value arrives, one realtime publish request. It does not switch AC or DC
outputs. `ANKER_SERIAL` limits the read to that station when the account has
more than one; an empty serial uses the A1790 on the account. Copy
`.env.example` to `.env` and fill it in. The password is not printed. Since
Anker app 3.10 the same account can stay logged in on the phone.

A Home Power Panel takes the F3800 off its own Wi-Fi and publishes through
the panel
([anker-solix-api issue 117](https://github.com/thomluther/anker-solix-api/issues/117)).
`cloud` still looks for product code A1790 or A1790P on the account.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m anker_monitor cloud
.venv/bin/python -m anker_monitor scan
.venv/bin/python -m anker_monitor read --timeout 30 --watch 5
.venv/bin/python -m anker_monitor lan
```

`cloud` is the status read once the station is on Wi-Fi. `scan` and `read`
need the computer radio on (`bluetoothctl power on`) and the station off
Wi-Fi. Press the IoT button until its icon is on the screen, then start
`read`. `--watch 5` keeps that Bluetooth session open and prints again every
5 seconds, until Ctrl-C. A `read` without `--watch` disconnects after one
sample and exits with status 1 when the handshake finishes and no status
frame arrives. `cloud` exits with status 1 when the account is valid and no
status frame is published. The cloud values are the same tags as the
Bluetooth frame: state of charge, watts, and temperature. Pack voltage is
not among them. The library stores the numbers as text; the printer accepts
those strings.

# Collect both

`collector` holds one Husky session and one F3800 Bluetooth session, using the
same `.env` identities as the two monitors. Every
interval it polls the Husky and reads the latest F3800 sample, then appends
two NDJSON lines. The default interval is 60 seconds. `--interval 300` is
five minutes. Blank fields are left out: missing numbers, empty text, an
F3800 value the station did not send, a Husky cell at 0 V, and a Husky
temperature probe at 0 °C. A real zero, such as 0 A or 0 W, is kept.

Lines for a local day go in `data/YYYY/MM/DD/readings.ndjson`. A run that
crosses midnight closes that file and opens the next day. `data/` is
gitignored.

Disconnect the phone app from both batteries. Press the F3800 IoT button
until its icon is showing, then start the collector. Ctrl-C closes both
radios. A device that is down is recorded as a short `error` line. If the
F3800 handshake stays up and no status frame arrives, that link is closed
and the next cycle scans and connects again. While the station is down, the
collector prints a reminder to short-press the IoT button until the
Bluetooth icon appears. It does not wait for a keypress. Holding that button
more than 7 seconds clears the network pairing. The Husky sample is taken
before the F3800 scan. That scan can still disturb the Husky session.

```bash
.venv/bin/python -m collector --interval 60
.venv/bin/python -m collector --interval 300
./status.sh
```

`status.sh` reads the newest Husky line and the newest Solix line that
contain a reading, including from the previous day when today has none yet.

# Capacity and runtime

`capacity` reads every `data/**/readings.ndjson` file. The Solix percentage
is its own 75 Ah / 3840 Wh. The Husky's 100 Ah / 5120 Wh is on the same bus
and is absent from that percentage. The bus voltage in the log is the Husky
reading. The Solix reports no pack voltage.

The Husky 2 operating window is 48–57 V, with a suggested low cutoff of
50.8 V and a BMS cutoff of 42–47 V. Its charge window is 55.6–57 V. The
F3800 publishes 51.2 V and 75 Ah and does not publish a pack cutoff, so
50.8 V is the shared stop on this pair. From about 3.20 V to 3.33 V per
cell the voltage barely moves, so the lowest cell is checked against
3.175 V and is not turned into a percentage. A drop while the Solix
percentage holds still, just after charging, is surface charge leaving.

Remaining Solix watt-hours follow the Solix percentage on the 75 Ah
nameplate. Amp-hours inferred for the Solix are the bus minus the Husky,
and they do not replace that nameplate. The Husky percentage is not
calibrated. Remaining Husky watt-hours are amp-hours counted from the last
rested full cell, when the Solix is near 100% and the lowest cell is at or
above 3.30 V. A gap is not filled in.

Runtime is the remaining DC watt-hours, times a 90% inverter, divided by an
AC load. The station's own hours-remaining figure is printed beside it and
does not include the Husky.

```bash
.venv/bin/python -m capacity
.venv/bin/python -m capacity --load 500
```

## Sources examined for the F3800

Bluetooth session. SolixBLE performs the handshake and parses `c840` when
the station sends it:

- [flip-dots/SolixBLE](https://github.com/flip-dots/SolixBLE) at commit
  [`1659309002a85a76405841323daf274a5a4c20ce`](https://github.com/flip-dots/SolixBLE/commit/1659309002a85a76405841323daf274a5a4c20ce)
  (version `4.0.0b2`). PyPI `3.9.0` sends a canned confirmation this station
  ignores, so the requirement is that commit.
  [`SolixBLE/devices/f3800.py`](https://github.com/flip-dots/SolixBLE/blob/1659309002a85a76405841323daf274a5a4c20ce/SolixBLE/devices/f3800.py)
  subscribes with command `4040` and parses telemetry `c840`. The same class
  can switch AC and DC outputs (`404a`, `404b`); those methods are not called.
  [`SolixBLE/advertisement.py`](https://github.com/flip-dots/SolixBLE/blob/1659309002a85a76405841323daf274a5a4c20ce/SolixBLE/advertisement.py)
  parses manufacturer id `0xFFFF`. The advertisement from this station is
  shorter than that parser accepts, so the matcher here accepts the short form.
- [SolixBLE issue 63](https://github.com/flip-dots/SolixBLE/issues/63)
  describes a newer F3800 key exchange (`4022` / `4822` / `4023` / `4823`)
  and the app's later `4040`, `4074`, and `4030` before `c840`. This station
  still answered the older public-key step and then sent no telemetry. The
  unpublished key exchange is not implemented.
- [flip-dots/HaSolixBLE](https://github.com/flip-dots/HaSolixBLE) README:
  Bluetooth and Wi-Fi cannot be used together.
- [moag1000/anker-solix-api-exploration](https://github.com/moag1000/anker-solix-api-exploration),
  from a decompile of Anker app 3.24.3:
  [`ble/handshake.md`](https://github.com/moag1000/anker-solix-api-exploration/blob/main/ble/handshake.md),
  [`ble/a1790.md`](https://github.com/moag1000/anker-solix-api-exploration/blob/main/ble/a1790.md),
  [`ble/protocol.md`](https://github.com/moag1000/anker-solix-api-exploration/blob/main/ble/protocol.md),
  and the tag tables on
  [`mqtt/a1790.md`](https://github.com/moag1000/anker-solix-api-exploration/blob/main/mqtt/a1790.md).
  Opcode `0040` is the empty F3800 status request (`getAllDeviceInfo`).
  Opcode `0057` is the realtime trigger. Output, light, screen, and bind
  commands listed there are not sent.

Cloud MQTT, which is the read used by `cloud`:

- [thomluther/anker-solix-api](https://github.com/thomluther/anker-solix-api)
  tag [`v3.9.0`](https://github.com/thomluther/anker-solix-api/releases/tag/v3.9.0).
  The [README](https://github.com/thomluther/anker-solix-api/blob/v3.9.0/README.md)
  separates the cloud web API from MQTT and documents `ANKERUSER`,
  `ANKERPASSWORD`, and `ANKERCOUNTRY`.
  [`src/anker_solix_api/mqttmap.py`](https://github.com/thomluther/anker-solix-api/blob/v3.9.0/src/anker_solix_api/mqttmap.py)
  `_A1790_0405` is the field map (`ad` charge, `a5`/`a6` AC power, `ae`/`af`/`b0`
  solar, `be` temperature).
  [`src/anker_solix_api/mqtt.py`](https://github.com/thomluther/anker-solix-api/blob/v3.9.0/src/anker_solix_api/mqtt.py)
  builds the status request `0040` and the realtime trigger `0057`.
  [`mqtt_monitor.py`](https://github.com/thomluther/anker-solix-api/blob/v3.9.0/mqtt_monitor.py)
  shows the subscribe-then-request sequence. Its output-control menu is not used.
- [Discussion 241](https://github.com/thomluther/anker-solix-api/discussions/241)
  is the F3800 owner capture those field names come from, including the note
  that tag `ad` matched the percentage in the app and tag `c0` did not.
- [Discussion 222](https://github.com/thomluther/anker-solix-api/discussions/222)
  is the MQTT decoding guide: one status request versus a realtime trigger
  that keeps publishing for about a minute.
- [ha-anker-solix issue 96](https://github.com/thomluther/ha-anker-solix/issues/96)
  records that a standalone F3800 cloud API response has firmware and Wi-Fi
  only, with partial MQTT added later in anker-solix-api 3.4.0.
- [anker-solix-api issue 117](https://github.com/thomluther/anker-solix-api/issues/117)
  records that an F3800 linked to a Home Power Panel drops its own Wi-Fi.

Local Modbus, checked and not used as a data source:

- [anker-charging/ha-anker-solix-official](https://github.com/anker-charging/ha-anker-solix-official)
  README and device list. The supported models are Solarbank, XE, smart plug,
  and smart meter. The F3800 is outside that list.
- Anker's Home Assistant page,
  <https://anker-webview-eu.anker.com/home-assistant>,
  for the same local API on port 502.
- [moag1000 `devices/modbus.md`](https://github.com/moag1000/anker-solix-api-exploration/blob/main/devices/modbus.md)
  for how the app enables third-party Modbus. No F3800 register map is
  published there.

# License

MIT. See [LICENSE](LICENSE).
