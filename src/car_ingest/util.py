# Format errors, series names and logging.

import datetime
import re
import sys


class FormatError(Exception):
  pass


def slug(name):
  return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def label(value):
  return value.replace("\\", "\\\\").replace('"', '\\"')


def series(name, unit, trip, car):
  return f'{name}{{unit="{label(unit)}",trip="{label(trip)}",car="{label(car)}"}}'


def log(message, err=False):
  print(
    f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} {message}",
    file=sys.stderr if err else sys.stdout,
    flush=True,
  )
