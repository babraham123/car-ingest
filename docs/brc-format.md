# Car Scanner `.brc` format

Car Scanner Pro saves each recording as a `.brc` file. The format is undocumented; this is
decoded from a real recording (`2026-05-24 20-10-53.brc`, a Kia EV6 over a vLinker MC), and every
sample matched the app's CSV export of the same recording: 3587 samples, same count and
timestamps (first sample 20:12:09.4 local).

It is .NET `BinaryWriter` output, little-endian; strings are 7-bit-varint length-prefixed UTF-8.

| Part | Layout |
| --- | --- |
| Header | string `"CARSCANNERRECORD"`, int32 version (`2`), strings VIN, car name, car profile, adapter (`BT vLinker MC-Android [MAC]`), int64 .NET `DateTime` ticks of recording start |
| Sensor def | uint32 `0x00D56190`, int32 sensor id, string name, string short name, int32 unit enum |
| Sample | uint32 `0x01274350`, float64 seconds since start, int32 sensor id, float64 value |

- After the header, defs and samples interleave. A def precedes its sensor's first sample.
  `car_ingest.py` fails the file on any other marker, a sample for an undefined sensor, a
  truncated record, or a recording without samples.
- The start time's `DateTime` Kind is Unspecified: it is the phone's local time. The top two bits
  of the int64 hold the Kind and are masked off. Sample time = start + offset.
- Values are SI regardless of the app's display units (°C, not the CSV's °F). Non-finite values
  are skipped.
- Unit enums seen: 0 none, 3 distance (probably km; the sample only has zeros), 6 rpm, 10 V, 14 %,
  15 °C, 17 s, 18 min, 27 kOhm, 30 Hz, 46 kW, 54 h, 58 currency, 59 A, 60 kWh, 61 Wh, 62 Ah,
  63 hours. An unknown enum becomes `unit="unknown_<n>"` rather than failing the file.
- The header's VIN is not published.

## Series

Each sample becomes `car_<sensor>{unit="…",trip="<file stem>",car="<default car>"}`, where
`<sensor>` is the sensor name slugified: `[BMS] Battery Power` → `car_bms_battery_power`. Car
Scanner names recordings by their start in local time (`%Y-%m-%d %H-%M-%S`), so the stem is the
trip label.
