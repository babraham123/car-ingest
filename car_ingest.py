#!/usr/bin/env python3
# Imports car data from a directory of uploads into VictoriaMetrics, at the samples'
# original timestamps: Car Scanner recordings (.brc) and WiCAN Pro logger batches
# (.jsonl, .jsonl.gz). Each file is imported once, keyed by sha256 in the state file, so a
# re-upload adds nothing. Malformed files move to failed/; a file whose oldest sample is
# past VictoriaMetrics' retention stays where it is, recorded as too_old. The files stay
# in the directory as the long-term archive. Standard library only.
# Every series carries a `car` label and a `trip` label. A .brc file is one trip, named by
# its stem. WiCAN rows are split into trips by gaps of more than 10 min, each named by its
# first row in local time, the shape of Car Scanner's stems; a batch continues its car's
# last trip when it starts within 10 min of it.
# The .brc format is decoded in docs/brc-format.md: .NET BinaryWriter output,
# little-endian, strings 7-bit-varint length-prefixed UTF-8. A header, then sensor
# definitions and samples interleaved, each behind a marker. Anything else fails the file.
# The JSONL rows are the WiCAN data logger's, as the sync app exports them
# (docs/wican-jsonl.md).
# Usage:
#   car_ingest.py [OPTIONS]        # one run over --car-dir
#   car_ingest.py [OPTIONS] FILE   # prints FILE's import lines, or why it fails
# Every option has an environment variable (car_ingest.py --help); the VictoriaMetrics
# password is CAR_INGEST_VM_PASSWORD only, so it never shows in a process list.

import argparse
import base64
import dataclasses
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
import zoneinfo
from collections import Counter
from pathlib import Path

IMPORT_PATH = "/api/v1/import/prometheus"
RESULTS = ("ok", "failed", "too_old")

MAGIC = "CARSCANNERRECORD"
DEF_MARKER = 0x00D56190
SAMPLE_MARKER = 0x01274350
# Car Scanner's unit enum; values are SI whatever the app displays.
# fmt: off
UNITS = {
  0: "none", 3: "km", 6: "rpm", 10: "V", 14: "%", 15: "°C", 17: "s", 18: "min",
  27: "kOhm", 30: "Hz", 46: "kW", 54: "h", 58: "currency", 59: "A", 60: "kWh",
  61: "Wh", 62: "Ah", 63: "hours",
}
# fmt: on
DOTNET_EPOCH = datetime.datetime(1, 1, 1)

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
# fmt: off
WICAN_UNITS = {
  "SPEED": "km/h", "TMP_A": "°C", "T_CAB": "°C", "REGEN_MAX": "kW", "POWER_MAX": "kW",
  "HV_H_1": "°C", "HV_KWH_R": "Wh", "SOH": "%", "HV_C_D_MIN_NO": "none",
  "HV_C_D_MAX_NO": "none", "HV_AH_CHARGED": "Ah", "HV_AH_DISCHARGED": "Ah",
  "DM_R_RPM": "rpm", "CAPACITOR": "V", "RESISTANCE": "kOhm", "HV_C_V_MAX_NO": "none",
  "HV_C_V_MIN_NO": "none", "OP_TIME": "hours", "CHARGING_R_A": "A", "LV_SOC": "%",
  "TYRE_P_FL": "psi", "TYRE_P_FR": "psi", "TYRE_P_RL": "psi", "TYRE_P_RR": "psi",
  "TYRE_T_FL": "°C", "TYRE_T_FR": "°C", "TYRE_T_RL": "°C", "TYRE_T_RR": "°C",
  "ODOMETER_MI": "mi", "ODOMETER": "km", "STEER_ANGLE": "deg",
}
# fmt: on
# ts_quality values whose ts is imported; "unknown" rows have no ts and are dropped
IMPORTED_QUALITIES = ("journal", "device", "anchor")
TRIP_GAP_MS = 10 * 60 * 1000
# <source>-<cursor>-<sha256 prefix>.jsonl[.gz], as the sync app names its batches
BATCH_NAME = re.compile(r"(?P<source>.+)-(?P<epoch>[0-9]+)_(?P<offset>[0-9]+)-[0-9a-f]+")


@dataclasses.dataclass
class Config:
  car_dir: Path = Path("/car")
  # Holds `state` (TSV: sha256, result, samples, unix time, file name) and `trips`
  state_dir: Path = Path("/state")
  metrics_file: Path = Path("/metrics/car_ingest.prom")
  # The phone's; recording start times are phone-local, and trips are named in it
  timezone: zoneinfo.ZoneInfo = zoneinfo.ZoneInfo("UTC")
  retention_days: int = 31
  # The car label of .brc files and of batches not named <source>-<cursor>-<hash>
  default_car: str = "car"
  # Batch source (dongle id) -> car label; an unmapped source is its own car
  car_sources: dict = dataclasses.field(default_factory=dict)


class FormatError(Exception):
  pass


class Reader:
  def __init__(self, data):
    self.data = data
    self.pos = 0

  def take(self, fmt):
    size = struct.calcsize(fmt)
    if self.pos + size > len(self.data):
      raise FormatError(f"truncated at byte {self.pos}")
    value = struct.unpack_from(fmt, self.data, self.pos)
    self.pos += size
    return value[0]

  def string(self):
    length, shift = 0, 0
    while True:
      byte = self.take("<B")
      length |= (byte & 0x7F) << shift
      shift += 7
      if not byte & 0x80:
        break
    if self.pos + length > len(self.data):
      raise FormatError(f"truncated string at byte {self.pos}")
    try:
      value = self.data[self.pos : self.pos + length].decode("utf-8")
    except UnicodeDecodeError as e:
      raise FormatError(f"bad string at byte {self.pos}: {e}") from e
    self.pos += length
    return value

  def done(self):
    return self.pos == len(self.data)


def slug(name):
  return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def label(value):
  return value.replace("\\", "\\\\").replace('"', '\\"')


def series(name, unit, trip, car):
  return f'{name}{{unit="{label(unit)}",trip="{label(trip)}",car="{label(car)}"}}'


def parse_brc(data, tz):
  """Returns the recording's samples as (name, unit, value, ms) tuples; raises FormatError."""
  r = Reader(data)
  if r.string() != MAGIC:
    raise FormatError("not a Car Scanner recording")
  version = r.take("<i")
  if version != 2:
    raise FormatError(f"unsupported version {version}")
  for _ in ("vin", "car name", "car profile", "adapter"):
    r.string()
  # The top two bits of a .NET DateTime hold its Kind.
  ticks = r.take("<q") & 0x3FFFFFFFFFFFFFFF
  start = (
    (DOTNET_EPOCH + datetime.timedelta(microseconds=ticks // 10)).replace(tzinfo=tz).timestamp()
  )

  sensors, samples = {}, []
  while not r.done():
    marker = r.take("<I")
    if marker == DEF_MARKER:
      sensor_id = r.take("<i")
      name = r.string()
      r.string()  # short name
      unit = r.take("<i")
      sensors[sensor_id] = ("car_" + slug(name), UNITS.get(unit, f"unknown_{unit}"))
    elif marker == SAMPLE_MARKER:
      offset = r.take("<d")
      sensor_id = r.take("<i")
      value = r.take("<d")
      if sensor_id not in sensors:
        raise FormatError(f"sample for undefined sensor {sensor_id}")
      if math.isfinite(value):
        samples.append((*sensors[sensor_id], value, round((start + offset) * 1000)))
    else:
      raise FormatError(f"unknown marker {marker:#010x} at byte {r.pos - 4}")
  if not samples:
    raise FormatError("no samples")
  return samples


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


def convert(path, data, trips, cfg):
  """FILE's samples as (series, value, ms), its car, and how many rows it dropped. trips is
  {car: (ms, trip)}, the last trip per car, updated in place for batches."""
  if not is_batch(path):
    samples = parse_brc(data, cfg.timezone)
    return (
      [(series(n, u, path.stem, cfg.default_car), v, ms) for n, u, v, ms in samples],
      cfg.default_car,
      0,
    )
  samples, dropped = parse_jsonl(data)
  car, source = batch_car(batch_stem(path), cfg)
  if source is not None and source not in cfg.car_sources:
    log(f"{path.name}: dongle {source} isn't in the car sources; labelled car={source}", err=True)
  labelled, trips[car] = split_trips(samples, trips.get(car), cfg.timezone)
  return [(series(n, u, trip, car), v, ms) for n, u, v, ms, trip in labelled], car, dropped


def import_lines(samples):
  """VictoriaMetrics' import format: Prometheus text with millisecond timestamps."""
  return "".join(f"{series} {value!r} {ms}\n" for series, value, ms in samples)


def log(message, err=False):
  print(
    f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} {message}",
    file=sys.stderr if err else sys.stdout,
    flush=True,
  )


def ingest(cfg, post, now):
  """Imports every settled file in cfg.car_dir not yet in the state file. post(body)
  raises on failure, which stops the run before the file is recorded, so the next run
  retries it."""
  car_dir, state = cfg.car_dir, cfg.state_dir / "state"
  oldest_ms = (now - cfg.retention_days * 86400) * 1000
  (car_dir / "failed").mkdir(parents=True, exist_ok=True)
  cfg.state_dir.mkdir(parents=True, exist_ok=True)
  state.touch()
  seen = {line.split("\t")[0] for line in state.read_text().splitlines()}
  # TSV: car, the last imported row's ms, its trip
  trips_file = cfg.state_dir / "trips"
  trips = {}
  if trips_file.exists():
    for line in trips_file.read_text().splitlines():
      name, ms, trip = line.split("\t")
      trips[name] = (int(ms), trip)

  def record(sha, result, samples, name):
    with state.open("a") as f:
      f.write(f"{sha}\t{result}\t{samples}\t{int(now)}\t{name}\n")
    seen.add(sha)
    log(f"{name}: {result} ({samples} samples)")

  def save_trips():
    tmp = trips_file.with_suffix(".tmp")
    tmp.write_text("".join(f"{name}\t{ms}\t{trip}\n" for name, (ms, trip) in sorted(trips.items())))
    tmp.replace(trips_file)

  # Each car's batches in cursor order, so its trips continue across them
  for path in sorted(car_dir.iterdir(), key=file_order):
    # Files modified in the last minute may still be uploading.
    if not path.is_file() or path.stat().st_mtime > now - 60:
      continue
    if path.suffix.lower() != ".brc" and not is_batch(path):
      continue
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha in seen:
      continue
    # Trips change only once the file is imported.
    pending = dict(trips)
    try:
      samples, _, dropped = convert(path, data, pending, cfg)
    except FormatError as e:
      log(f"{path.name}: {e}", err=True)
      shutil.move(path, car_dir / "failed" / path.name)
      record(sha, "failed", 0, path.name)
      continue
    if dropped:
      log(f"{path.name}: dropped {dropped} rows with an unknown time")
    # VictoriaMetrics silently drops samples outside its retention.
    if min(ms for _, _, ms in samples) < oldest_ms:
      record(sha, "too_old", len(samples), path.name)
      continue
    post(import_lines(samples).encode())
    if pending != trips:
      trips = pending
      save_trips()
    record(sha, "ok", len(samples), path.name)

  counts = Counter(line.split("\t")[1] for line in state.read_text().splitlines())
  lines = [
    "# HELP car_ingest_files_total Car recordings and logger batches processed, by outcome.",
    "# TYPE car_ingest_files_total counter",
  ]
  lines += ['car_ingest_files_total{result="%s"} %d' % (r, counts[r]) for r in RESULTS]
  lines += [
    "# HELP car_ingest_last_success_timestamp_seconds Unix time of the last car_ingest run that completed.",
    "# TYPE car_ingest_last_success_timestamp_seconds gauge",
    f"car_ingest_last_success_timestamp_seconds {int(now)}",
  ]
  # node_exporter's textfile collector only reads *.prom, so the rename is atomic for it.
  metrics = cfg.metrics_file
  metrics.parent.mkdir(parents=True, exist_ok=True)
  tmp = metrics.with_suffix(".tmp")
  tmp.write_text("\n".join(lines) + "\n")
  tmp.chmod(0o644)
  tmp.replace(metrics)


def poster(url, user, password):
  """post(body) for VictoriaMetrics at URL, with basic auth when password is set; retries
  server errors and connection failures 3 times, 10 s apart."""
  headers = {}
  if password:
    headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

  def post(body):
    request = urllib.request.Request(url.rstrip("/") + IMPORT_PATH, data=body, headers=headers)
    for attempt in range(4):
      try:
        urllib.request.urlopen(request, timeout=120).close()
        return
      except urllib.error.HTTPError as e:
        if e.code < 500 or attempt == 3:
          raise
      except OSError:
        if attempt == 3:
          raise
      time.sleep(10)

  return post


def car_sources(text):
  """SOURCE=CAR pairs, comma- or whitespace-separated, as a dict."""
  sources = {}
  for pair in re.split(r"[,\s]+", text.strip()):
    if not pair:
      continue
    source, sep, car = pair.partition("=")
    if not sep or not source or not car:
      raise ValueError(f"not SOURCE=CAR: {pair!r}")
    sources[source] = car
  return sources


def parse_args(argv, env):
  p = argparse.ArgumentParser(
    prog="car_ingest.py",
    description="Imports Car Scanner .brc recordings and WiCAN Pro .jsonl[.gz] batches into "
    "VictoriaMetrics at their original timestamps. Each option defaults to the "
    "environment variable named in its help. The VictoriaMetrics password is "
    "CAR_INGEST_VM_PASSWORD.",
  )
  defaults = Config()

  def opt(flag, var, default, help, **kwargs):
    p.add_argument(
      flag, default=env.get(var, default), help=f"{help} [{var}, default: %(default)s]", **kwargs
    )

  opt("--car-dir", "CAR_INGEST_CAR_DIR", str(defaults.car_dir), "directory of uploads")
  opt(
    "--state-dir",
    "CAR_INGEST_STATE_DIR",
    str(defaults.state_dir),
    "where the imported-file and trip state lives",
  )
  opt(
    "--metrics-file",
    "CAR_INGEST_METRICS_FILE",
    str(defaults.metrics_file),
    "Prometheus textfile the run's counters are written to",
  )
  opt(
    "--vm-url",
    "CAR_INGEST_VM_URL",
    None,
    "VictoriaMetrics base URL, e.g. https://metrics.example.com",
  )
  opt(
    "--vm-user",
    "CAR_INGEST_VM_USER",
    "admin",
    "basic auth user, sent when CAR_INGEST_VM_PASSWORD is set",
  )
  opt(
    "--timezone",
    "CAR_INGEST_TIMEZONE",
    env.get("TZ") or "UTC",
    "IANA zone the phone records in and trips are named in; falls back to TZ",
  )
  opt(
    "--retention-days",
    "CAR_INGEST_RETENTION_DAYS",
    str(defaults.retention_days),
    "VictoriaMetrics' retention; older files are recorded as too_old",
  )
  opt(
    "--default-car",
    "CAR_INGEST_DEFAULT_CAR",
    defaults.default_car,
    "car label for .brc files and batches not named <source>-<cursor>-<hash>",
  )
  opt(
    "--car-sources",
    "CAR_INGEST_CAR_SOURCES",
    "",
    "comma-separated SOURCE=CAR pairs naming each WiCAN dongle's car",
  )
  p.add_argument("file", nargs="?", type=Path, help="print FILE's import lines instead of running")
  args = p.parse_args(argv)

  try:
    timezone = zoneinfo.ZoneInfo(args.timezone)
  except (zoneinfo.ZoneInfoNotFoundError, ValueError):
    p.error(f"unknown timezone {args.timezone!r}")
  try:
    retention_days = int(args.retention_days)
    sources = car_sources(args.car_sources)
  except ValueError as e:
    p.error(str(e))
  if args.file is None and not args.vm_url:
    p.error("--vm-url (CAR_INGEST_VM_URL) is required to run")
  cfg = Config(
    Path(args.car_dir),
    Path(args.state_dir),
    Path(args.metrics_file),
    timezone,
    retention_days,
    args.default_car,
    sources,
  )
  return cfg, args


def main():
  cfg, args = parse_args(sys.argv[1:], os.environ)
  if args.file is not None:
    try:
      samples, _, dropped = convert(args.file, args.file.read_bytes(), {}, cfg)
    except FormatError as e:
      sys.exit(f"{args.file}: {e}")
    if dropped:
      print(f"{args.file}: dropped {dropped} rows with an unknown time", file=sys.stderr)
    sys.stdout.write(import_lines(samples))
    return
  post = poster(args.vm_url, args.vm_user, os.environ.get("CAR_INGEST_VM_PASSWORD", ""))
  try:
    ingest(cfg, post, time.time())
  except OSError as e:
    sys.exit(f"stopped, the next run retries: {e}")


if __name__ == "__main__":
  main()
