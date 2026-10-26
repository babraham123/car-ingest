# WiCAN Pro logger batches, the rows the sync app exports (docs/wican-jsonl.md), and the
# trips and car they belong to.

import datetime
import gzip
import json
import math
import re

from .util import FormatError, slug

# WiCAN params mapped onto the Car Scanner series and unit labels, so one dashboard serves
# both: param -> (series, unit, divisor)
WICAN_SERIES = {
  "SOC": ("car_bms_state_of_charge_bms", "%", 1),
  "SOC_D": ("car_bms_state_of_charge_display", "%", 1),
  "HV_W": ("car_bms_battery_power", "kW", 1000),
  "HV_A": ("car_bms_battery_current", "A", 1),
  "HV_V": ("car_bms_battery_dc_voltage", "V", 1),
  "HV_C_V_MAX": ("car_bms_maximum_cell_voltage", "V", 1),
  "HV_C_V_MIN": ("car_bms_minimum_cell_voltage", "V", 1),
  "HV_C_DIFF": ("car_bms_battery_cell_voltage_deviation", "V", 1),
  "HV_T_MAX": ("car_bms_battery_max_temperature", "°C", 1),
  "HV_T_MIN": ("car_bms_battery_min_temperature", "°C", 1),
  "KWH_CHARGED": ("car_bms_cumulative_energy_charged", "kWh", 1),
  "KWH_DISCHARGED": ("car_bms_cumulative_energy_discharged", "kWh", 1),
  "LV_V": ("car_bms_auxillary_battery_voltage", "V", 1),
}
WICAN_SERIES.update(
  {f"HV_T_{n}": (f"car_bms_battery_pack_b{n:02d}", "°C", 1) for n in range(1, 17)}
)
# Every other param's unit, from wican-fw's vehicle_profiles.json, "Kia: EV6"; an empty
# unit there is "unknown" here, like a param the profile lacks
WICAN_UNITS = {
  "SPEED": "km/h",
  "TMP_A": "°C",
  "T_CAB": "°C",
  "REGEN_MAX": "kW",
  "POWER_MAX": "kW",
  "HV_H_1": "°C",
  "HV_KWH_R": "Wh",
  "SOH": "%",
  "HV_C_D_MIN_NO": "none",
  "HV_C_D_MAX_NO": "none",
  "HV_AH_CHARGED": "Ah",
  "HV_AH_DISCHARGED": "Ah",
  "DM_R_RPM": "rpm",
  "CAPACITOR": "V",
  "RESISTANCE": "kOhm",
  "HV_C_V_MAX_NO": "none",
  "HV_C_V_MIN_NO": "none",
  "OP_TIME": "hours",
  "CHARGING_R_A": "A",
  "LV_SOC": "%",
  "TYRE_P_FL": "psi",
  "TYRE_P_FR": "psi",
  "TYRE_P_RL": "psi",
  "TYRE_P_RR": "psi",
  "TYRE_T_FL": "°C",
  "TYRE_T_FR": "°C",
  "TYRE_T_RL": "°C",
  "TYRE_T_RR": "°C",
  "ODOMETER_MI": "mi",
  "ODOMETER": "km",
  "STEER_ANGLE": "deg",
}
# ts_quality values whose ts is imported; "unknown" rows have no ts and are dropped
IMPORTED_QUALITIES = ("journal", "device", "anchor")
TRIP_GAP_MS = 10 * 60 * 1000
# <source>-<cursor>-<sha256 prefix>.jsonl[.gz], as the sync app names its batches
BATCH_NAME = re.compile(r"(?P<source>.+)-(?P<epoch>[0-9]+)_(?P<offset>[0-9]+)-[0-9a-f]+")


def parse_jsonl(data):
  """Returns a WiCAN batch's importable rows as (name, unit, value, ms) tuples, in time
  order, and how many unknown-time rows it dropped; raises FormatError."""
  if data[:2] == b"\x1f\x8b":
    try:
      data = gzip.decompress(data)
    except (OSError, EOFError) as e:
      raise FormatError(f"bad gzip: {e}") from e
  try:
    lines = data.decode("utf-8").splitlines()
  except UnicodeDecodeError as e:
    raise FormatError(f"not UTF-8: {e}") from e

  samples, dropped = [], 0
  for number, line in enumerate(lines, 1):
    try:
      row = json.loads(line)
    except ValueError as e:
      raise FormatError(f"line {number}: not JSON: {e}") from e
    if not isinstance(row, dict):
      raise FormatError(f"line {number}: not an object")
    # The batch's marker and meta lines
    if set(row) == {"_file"} or "_cursor" in row:
      continue
    param, value, ts, quality = (row.get(k) for k in ("param", "value", "ts", "ts_quality"))
    if not isinstance(param, str) or "." not in param:
      raise FormatError(f"line {number}: no <source>.<name> param")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      raise FormatError(f"line {number}: value isn't a number")
    if quality == "unknown":
      dropped += 1
      continue
    if quality not in IMPORTED_QUALITIES:
      raise FormatError(f"line {number}: unexpected ts_quality {quality!r}")
    if isinstance(ts, bool) or not isinstance(ts, int):
      raise FormatError(f"line {number}: {quality} row without an integer ts")
    if not math.isfinite(value):
      continue
    name = param.split(".", 1)[1]
    series_name, unit, divisor = WICAN_SERIES.get(
      name, ("car_" + slug(name), WICAN_UNITS.get(name) or "unknown", 1)
    )
    samples.append((series_name, unit, float(value) / divisor, ts))
  if not samples:
    raise FormatError("no importable rows")
  samples.sort(key=lambda s: s[3])
  return samples, dropped


def batch_car(stem, cfg):
  """The car a batch named STEM (without .jsonl[.gz]) belongs to, and its source id."""
  match = BATCH_NAME.fullmatch(stem)
  if not match:
    return cfg.default_car, None
  source = match["source"]
  return cfg.car_sources.get(source, source), source


def file_order(path):
  """Sorts each dongle's batches by cursor, compared as numbers: offsets aren't padded."""
  match = BATCH_NAME.fullmatch(batch_stem(path)) if is_batch(path) else None
  if not match:
    return path.name, 0, 0, path.name
  return match["source"], int(match["epoch"]), int(match["offset"]), path.name


def trip_name(ms, tz):
  return datetime.datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H-%M-%S")


def split_trips(samples, last, tz):
  """Labels time-ordered samples with trips. last is the car's (ms, trip) before this
  batch, or None. Returns [(name, unit, value, ms, trip)] and the new last."""
  out = []
  for name, unit, value, ms in samples:
    if last is None or abs(ms - last[0]) > TRIP_GAP_MS:
      last = (ms, trip_name(ms, tz))
    last = (ms, last[1])
    out.append((name, unit, value, ms, last[1]))
  return out, last


def is_batch(path):
  return path.name.lower().endswith((".jsonl", ".jsonl.gz"))


def batch_stem(path):
  return re.sub(r"\.jsonl(\.gz)?$", "", path.name, flags=re.IGNORECASE)
