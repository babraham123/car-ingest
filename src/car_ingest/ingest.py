# One file's conversion, and a run over the upload directory with its state and metrics.

import hashlib
import shutil
from collections import Counter

from .brc import parse_brc
from .util import FormatError, log, series
from .vm import import_lines
from .wican import batch_car, batch_stem, file_order, is_batch, parse_jsonl, split_trips

RESULTS = ("ok", "failed", "too_old")


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
