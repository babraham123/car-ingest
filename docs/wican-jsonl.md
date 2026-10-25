# WiCAN Pro logger batches (JSONL)

A WiCAN Pro OBD dongle polls the car itself and logs to its SD card. A phone app (the WiCAN Pro
BLE-sync app, a separate GPL-3.0 Android repo) pulls the log over BLE and uploads it in batches.

## Upload format (decided by the app)

- One file per batch, gzipped, named `<source>-<cursor>-<sha256 prefix>.jsonl.gz`.
  `car_ingest.py` also takes plain `.jsonl`, and recognises gzip by its magic bytes.
- `<source>` is the dongle's id, `wican:<BLE MAC>`, with every character outside
  `[A-Za-z0-9._-]` replaced by `_`: `wican_AA_BB_CC_DD_EE_01-1784000060_0-0123456789abcdef.jsonl.gz`.
  The cursor is `<epoch>_<offset>` and neither it nor the hash contains a `-`.
- A batch is a sealed slice of
  `GET /api/logger/export?format=jsonl&stream=params&since=<cursor>&limit=N`, cut at the logger's
  `_file` boundaries. Batches are disjoint: each row is in exactly one batch, and a re-sent batch
  is byte-identical, so the sha256 dedup catches it and VictoriaMetrics needs no
  `-dedup.minScrapeInterval`.
- Lines (row shape per meatpi-components `components/data_logger/README.md`):
  - first, `{"_file":"dl_<epoch>.jsonl"}`;
  - rows: `{"ts":<epoch ms>,"param":"<source>.<name>","value":<number>,"ts_quality":"…",
    "ts_raw":<epoch ms>}`, e.g. `"param":"autopid.SOC"`. `ts_raw` is the device's original
    stamp; `ts` is the app's corrected time, and `ts_quality` says how it got it:

    | `ts_quality` | `ts` | Imported |
    | --- | --- | --- |
    | `journal` | a clock-journal step (`/sd/logs/clock.jsonl`) applied to `ts_raw` | yes |
    | `device` | `ts_raw` as-is: plausible, kept (most rows) | yes |
    | `anchor` | an implausible `ts_raw` corrected by the phone/device clock offset | yes |
    | `unknown` | `null` | no |

  - possibly a trailing `{"_cursor":"<epoch>:<offset>","more":<bool>}` meta line.
- Param names are WiCAN's, unchanged. The app is device-agnostic and never renames them.

## Import

- `_file` and `_cursor` lines are skipped; any other line that isn't a row fails the file.
  `ts_raw` is ignored.
- `unknown` rows are dropped and counted in the log; a label for them would split every series.
  Any other `ts_quality`, a non-integer `ts` with an imported quality, or a batch with no
  importable rows fails the file.
- The params a Car Scanner dashboard charts map onto the Car Scanner series, so one dashboard
  serves both sources. Units come from wican-fw `vehicle_profiles.json` (`Kia: EV6`):

  | WiCAN param (unit) | Series (unit label) |
  | --- | --- |
  | `SOC` (%) | `car_bms_state_of_charge_bms` (%) |
  | `SOC_D` (%) | `car_bms_state_of_charge_display` (%) |
  | `HV_W` (W, ÷ 1000) | `car_bms_battery_power` (kW) |
  | `HV_A` (A) | `car_bms_battery_current` (A) |
  | `HV_V` (V) | `car_bms_battery_dc_voltage` (V) |
  | `HV_C_V_MAX` / `HV_C_V_MIN` (V) | `car_bms_maximum_cell_voltage` / `car_bms_minimum_cell_voltage` (V) |
  | `HV_C_DIFF` (V) | `car_bms_battery_cell_voltage_deviation` (V) |
  | `HV_T_MAX` / `HV_T_MIN` (°C) | `car_bms_battery_max_temperature` / `car_bms_battery_min_temperature` (°C) |
  | `HV_T_1`…`HV_T_16` (°C) | `car_bms_battery_pack_b01`…`b16` (°C) |
  | `KWH_CHARGED` / `KWH_DISCHARGED` (kWh) | `car_bms_cumulative_energy_charged` / `…_discharged` (kWh) |
  | `LV_V` (V) | `car_bms_auxillary_battery_voltage` (V) |

  Any other param is `car_<slug(name)>` with its profile unit, or `unknown` if the profile has
  none. `car_bms_battery_inlet_temperature` and `car_obd_module_voltage` have no WiCAN source and
  come only from `.brc` recordings.

## `trip` label

The app has no notion of trips. A trip starts at the first row after a gap of more than 10 min,
which covers a trip spread over several batches and a batch holding several trips. The label is
the trip's first row in local time (`--timezone`), formatted `%Y-%m-%d %H-%M-%S`: the shape of Car
Scanner's file stems, so a trip selector lists both sources the same way.

The state directory's `trips` file holds each car's last imported row time and trip (TSV: car,
ms, trip), written only after a successful import, so the next batch from that car continues its
trip if it starts within 10 min. Each dongle's batches are imported in cursor order, the offset
compared as a number.

## `car` label

Every series carries a `car` label, so two cars never share a series or a trip. Rows carry no
source, so it comes from the batch's file name: `--car-sources` maps `<source>` to a car name. An
unmapped source is labelled with the source itself and logged, so a new dongle shows up as its own
car. `.brc` files and batch names without the `<source>-<cursor>-<hash>` shape get
`--default-car`. The id is the dongle's, not the car's: moving a dongle to another car means
editing the map.

## Known dongle-side caveats

- **Sign:** `HV_A = ((S17*256)+B18)/10` is signed and `HV_W = HV_V × HV_A`, so both follow the
  BMS's sign convention for current. Compare them with Car Scanner's battery power (−12.25 kW in
  the reference recording) and negate in the mapping if the two disagree.
- **Cumulative counters:** wican-fw's EV6 profile misreads `KWH_DISCHARGED` and `HV_AH_CHARGED`.
  The fix belongs on the dongle, so ingest has no special case.
