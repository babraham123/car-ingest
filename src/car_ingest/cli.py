# Arguments, and the run or one file's import lines.
# Usage:
#   car_ingest [OPTIONS]        # one run over --car-dir
#   car_ingest [OPTIONS] FILE   # prints FILE's import lines, or why it fails
# Every option has an environment variable (car_ingest --help); the VictoriaMetrics
# password is CAR_INGEST_VM_PASSWORD only, so it never shows in a process list.

import argparse
import os
import sys
import time
import zoneinfo
from pathlib import Path

from .config import Config, car_sources
from .ingest import convert, ingest
from .util import FormatError
from .vm import import_lines, poster


def parse_args(argv, env):
  p = argparse.ArgumentParser(
    prog="car_ingest",
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


def main(argv):
  cfg, args = parse_args(argv, os.environ)
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


def entry():
  """The zipapp's entry point."""
  sys.exit(main(sys.argv[1:]))
