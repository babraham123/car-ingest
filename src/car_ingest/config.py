# The run's configuration.

import dataclasses
import re
import zoneinfo
from pathlib import Path


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
