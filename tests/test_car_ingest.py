# car_ingest.py on .brc files written to the format in docs/brc-format.md: header start
# time is phone-local, sample time = start + offset, sensor names slugified, unit enums
# mapped (unknown ones to unknown_<n>), the trip label is the file stem, the car label the
# default car, and any unexpected structure fails the file. On WiCAN batches
# (docs/wican-jsonl.md), plain or gzipped: marker and meta lines skipped,
# journal/device/anchor rows imported at ts, unknown rows dropped, the dashboard's params
# mapped onto the Car Scanner series, trips split by gaps of more than 10 min and continued
# across a car's batches, and the car label from the dongle id in the file name through
# the car sources. Each file is imported once; malformed files move to failed/; files past
# retention are too_old and never posted; a failed post records nothing, so the next run
# retries.
# Usage:
#   python3 -m unittest discover -s tests

import contextlib
import datetime
import gzip
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import car_ingest  # noqa: E402

TZ = "America/Los_Angeles"
# 2026-05-24 20:10:53 PDT, the recordings' start
START = 1779678653
RETENTION_DAYS = 14
# Dongle ids as the sync app writes them into batch names; only the first is a car source
EV6 = "wican_AA_BB_CC_DD_EE_01"
OTHER = "wican_AA_BB_CC_DD_EE_02"
# A day after the start: inside retention, and the upload long settled
DAY_LATER = START + 86400
TRIP = ["def:7:[BMS] Battery Power:46", "sample:1.5:7:-12.25", "sample:120.25:7:4.5"]


def string(s):
  b = s.encode()
  n, out = len(b), bytearray()
  while True:
    out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
    n >>= 7
    if not n:
      return bytes(out) + b


def brc(*records):
  """A recording started 2026-05-24 20:10:53 local time. RECORD is def:ID:NAME:UNIT,
  sample:OFFSET:ID:VALUE or marker:HEX."""
  ticks = (
    (datetime.datetime(2026, 5, 24, 20, 10, 53) - datetime.datetime(1, 1, 1))
    // datetime.timedelta(microseconds=1)
    * 10
  )
  out = string("CARSCANNERRECORD") + struct.pack("<i", 2)
  out += b"".join(string(s) for s in ("VIN0000", "My car", "Kia e-GMP", "BT vLinker MC"))
  out += struct.pack("<q", ticks)
  for record in records:
    kind, *f = record.split(":")
    if kind == "def":
      out += (
        struct.pack("<Ii", 0x00D56190, int(f[0]))
        + string(f[1])
        + string("short")
        + struct.pack("<i", int(f[2]))
      )
    elif kind == "sample":
      out += struct.pack("<Idid", 0x01274350, float(f[0]), int(f[1]), float(f[2]))
    else:
      out += struct.pack("<I", int(f[0], 16))
  return out


def row(seconds, param, value, quality="device"):
  """A logger row SECONDS after the start."""
  ms = (START + seconds) * 1000
  return json.dumps(
    {
      "ts": None if quality == "unknown" else ms,
      "param": f"autopid.{param}",
      "value": value,
      "ts_quality": quality,
      "ts_raw": ms,
    }
  )


def name(dongle, cursor, offset=0):
  """A batch file name as the sync app writes it."""
  return f"{dongle}-{cursor}_{offset}-0123456789abcdef.jsonl.gz"


class FailingPost(OSError):
  pass


class CarIngestTest(unittest.TestCase):
  def setUp(self):
    self.tmp = Path(tempfile.mkdtemp())
    self.addCleanup(shutil.rmtree, self.tmp)
    self.car = self.tmp / "car"
    self.cfg = car_ingest.Config(
      car_dir=self.car,
      state_dir=self.tmp / "state",
      metrics_file=self.tmp / "car_ingest.prom",
      timezone=zoneinfo.ZoneInfo(TZ),
      retention_days=RETENTION_DAYS,
      default_car="ev6",
      car_sources={EV6: "ev6"},
    )
    self.posted = []
    self.post_fails = False

  # Helpers

  def settle(self, path):
    """Last modified at the start, so a run a day later sees it settled."""
    os.utime(path, (START, START))

  def upload(self, file_name, *records):
    self.car.mkdir(parents=True, exist_ok=True)
    (self.car / file_name).write_bytes(brc(*records))
    self.settle(self.car / file_name)

  def batch(self, file_name, lines, gz=True):
    self.car.mkdir(parents=True, exist_ok=True)
    data = "".join(line + "\n" for line in lines).encode()
    (self.car / file_name).write_bytes(gzip.compress(data) if gz else data)
    self.settle(self.car / file_name)

  def convert(self, path):
    """car_ingest.py FILE through its command line: (exit status, stdout, stderr)."""
    env = {
      "PATH": os.environ.get("PATH", ""),
      "CAR_INGEST_TIMEZONE": TZ,
      "CAR_INGEST_DEFAULT_CAR": "ev6",
      "CAR_INGEST_CAR_SOURCES": f"{EV6}=ev6",
    }
    p = subprocess.run(
      [sys.executable, "-I", str(ROOT / "car_ingest.py"), str(path)],
      env=env,
      capture_output=True,
      text=True,
    )
    return p.returncode, p.stdout, p.stderr

  def converted(self, path):
    status, out, err = self.convert(path)
    self.assertEqual(status, 0, err)
    return out

  def assert_rejected(self, path, why):
    status, _, _ = self.convert(path)
    self.assertNotEqual(status, 0, why)

  def ingest(self, now):
    """Runs ingest() over car/; returns its (stdout, stderr)."""

    def post(body):
      if self.post_fails:
        raise FailingPost("connection refused")
      self.posted.append(body.decode())

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      car_ingest.ingest(self.cfg, post, float(now))
    return out.getvalue(), err.getvalue()

  def posted_lines(self):
    return "".join(self.posted).splitlines()

  def state(self):
    """Columns 2, 3 and 5 of the state file: result, samples, file name."""
    path = self.cfg.state_dir / "state"
    if not path.exists():
      return []
    return [
      "\t".join(line.split("\t")[i] for i in (1, 2, 4)) for line in path.read_text().splitlines()
    ]

  def trips_and_values(self):
    return [
      re.sub(r'.*trip="([^"]*)".* ([0-9.]+) [0-9]+$', r"\1 \2", line)
      for line in self.posted_lines()
    ]

  # .brc conversion

  def test_converts_samples_at_local_start_plus_offset(self):
    path = self.tmp / "trip1.brc"
    path.write_bytes(
      brc(
        "def:7:[BMS] Battery Power:46",
        "sample:1.5:7:-12.25",
        'def:9:Odd "Name" 2:99',
        "sample:2:9:3",
        "sample:3:9:nan",
        "sample:120.25:7:4.5",
      )
    )
    self.assertEqual(
      self.converted(path).splitlines(),
      [
        f'car_bms_battery_power{{unit="kW",trip="trip1",car="ev6"}} -12.25 {START * 1000 + 1500}',
        f'car_odd_name_2{{unit="unknown_99",trip="trip1",car="ev6"}} 3.0 {START * 1000 + 2000}',
        f'car_bms_battery_power{{unit="kW",trip="trip1",car="ev6"}} 4.5 {START * 1000 + 120250}',
      ],
    )

  def test_fails_unknown_marker(self):
    path = self.tmp / "t.brc"
    path.write_bytes(brc("def:7:Power:46", "sample:1:7:1", "marker:deadbeef"))
    self.assert_rejected(path, "accepted an unknown marker")

  def test_fails_sample_before_its_definition(self):
    path = self.tmp / "t.brc"
    path.write_bytes(brc("sample:1:7:1", "def:7:Power:46"))
    self.assert_rejected(path, "accepted an undefined sensor")

  def test_fails_truncated_file(self):
    path = self.tmp / "cut.brc"
    path.write_bytes(brc("def:7:Power:46", "sample:1:7:1")[:-3])
    self.assert_rejected(path, "accepted a truncated file")

  def test_fails_without_samples(self):
    path = self.tmp / "t.brc"
    path.write_bytes(brc("def:7:Power:46"))
    self.assert_rejected(path, "accepted a file without samples")

  def test_fails_other_files(self):
    path = self.tmp / "t.csv"
    path.write_text('"SECONDS";"PID";"VALUE";"UNITS";\n')
    self.assert_rejected(path, "accepted a CSV")

  # A run over the directory

  def test_imports_each_recording_once(self):
    self.upload("a.brc", *TRIP)
    self.ingest(DAY_LATER)
    # The same recording uploaded again under another name
    (self.car / "b.brc").write_bytes((self.car / "a.brc").read_bytes())
    self.settle(self.car / "b.brc")
    self.ingest(DAY_LATER + 900)
    self.assertEqual(
      self.posted_lines(),
      [
        f'car_bms_battery_power{{unit="kW",trip="a",car="ev6"}} -12.25 {START * 1000 + 1500}',
        f'car_bms_battery_power{{unit="kW",trip="a",car="ev6"}} 4.5 {START * 1000 + 120250}',
      ],
    )
    self.assertEqual(self.state(), ["ok\t2\ta.brc"])
    self.assertTrue((self.car / "a.brc").is_file(), "the recording left the directory")
    self.assertEqual(
      self.cfg.metrics_file.read_text(),
      f"""\
# HELP car_ingest_files_total Car recordings and logger batches processed, by outcome.
# TYPE car_ingest_files_total counter
car_ingest_files_total{{result="ok"}} 1
car_ingest_files_total{{result="failed"}} 0
car_ingest_files_total{{result="too_old"}} 0
# HELP car_ingest_last_success_timestamp_seconds Unix time of the last car_ingest run that completed.
# TYPE car_ingest_last_success_timestamp_seconds gauge
car_ingest_last_success_timestamp_seconds {DAY_LATER + 900}
""",
    )

  def test_moves_malformed_recordings_to_failed(self):
    self.upload("bad.brc", "sample:1:7:1")
    self.ingest(DAY_LATER)
    self.assertTrue((self.car / "failed" / "bad.brc").is_file())
    self.assertFalse((self.car / "bad.brc").exists())
    self.assertEqual(self.posted, [], "posted a malformed recording")
    self.assertEqual(self.state(), ["failed\t0\tbad.brc"])
    self.assertIn('car_ingest_files_total{result="failed"} 1\n', self.cfg.metrics_file.read_text())

  def test_records_trips_past_retention_as_too_old(self):
    self.upload("old.brc", *TRIP)
    self.ingest(START + (RETENTION_DAYS + 1) * 86400)
    self.assertEqual(self.posted, [], "posted samples VictoriaMetrics would drop")
    self.assertTrue((self.car / "old.brc").is_file(), "the recording left the directory")
    self.assertEqual(self.state(), ["too_old\t2\told.brc"])

  def test_retries_a_failed_post_on_the_next_run(self):
    self.upload("a.brc", *TRIP)
    self.post_fails = True
    with self.assertRaises(FailingPost):
      self.ingest(DAY_LATER)
    self.assertEqual(self.state(), [], "recorded a file that wasn't imported")
    self.assertFalse(self.cfg.metrics_file.exists(), "a failed run counted as a success")
    self.post_fails = False
    self.ingest(DAY_LATER + 900)
    self.assertEqual(self.state(), ["ok\t2\ta.brc"])

  def test_skips_recordings_still_uploading(self):
    self.upload("a.brc", *TRIP)
    # Modified 30 s before the run
    self.ingest(START + 30)
    self.assertEqual(self.state(), [])
    self.assertEqual(self.posted, [], "imported a recording still uploading")

  # WiCAN batches

  def test_imports_plain_and_gzipped_batches(self):
    self.batch(name(EV6, 1), ['{"_file":"dl_1779678653.jsonl"}', row(0, "SOC", 80)])
    self.batch(
      f"{EV6}-2_0-fedcba9876543210.jsonl",
      [row(60, "SOC", 79.5), '{"_cursor":"1779678713:2","more":false}'],
      gz=False,
    )
    self.ingest(DAY_LATER)
    self.assertEqual(
      self.posted_lines(),
      [
        f'car_bms_state_of_charge_bms{{unit="%",trip="2026-05-24 20-10-53",car="ev6"}} 80.0 {START * 1000}',
        f'car_bms_state_of_charge_bms{{unit="%",trip="2026-05-24 20-10-53",car="ev6"}} 79.5 {(START + 60) * 1000}',
      ],
    )

  def test_imports_rows_by_ts_quality(self):
    self.batch(
      name(EV6, 1),
      [
        row(0, "SOC", 80, "journal"),
        row(1, "SOC", 81),
        row(2, "SOC", 82, "anchor"),
        row(3, "SOC", 83, "unknown"),
      ],
    )
    out, _ = self.ingest(DAY_LATER)
    self.assertIn("dropped 1 rows with an unknown time", out)
    self.assertEqual(
      [" ".join(line.split(" ")[2:4]) for line in self.posted_lines()],
      [f"80.0 {START * 1000}", f"81.0 {(START + 1) * 1000}", f"82.0 {(START + 2) * 1000}"],
    )

  def test_fails_malformed_batches(self):
    path = self.tmp / "t.jsonl"
    for bad in (
      "not json",
      '{"param":"autopid.SOC"}',
      row(0, "SOC", 80, "guessed"),
      '{"ts":null,"param":"autopid.SOC","value":80,"ts_quality":"device"}',
      row(0, "SOC", True),
    ):
      path.write_text(row(0, "SOC", 80) + "\n" + bad + "\n")
      self.assert_rejected(path, f"accepted: {bad}")
    path.write_text(row(0, "SOC", 80, "unknown") + "\n")
    self.assert_rejected(path, "accepted a batch without importable rows")
    self.batch(name(EV6, 1), [row(0, "SOC", 80), "not json"])
    self.ingest(DAY_LATER)
    self.assertTrue((self.car / "failed" / name(EV6, 1)).is_file(), "not moved to failed/")
    self.assertEqual(self.posted, [])

  def test_maps_params_onto_car_scanner_series(self):
    path = self.tmp / "t.jsonl"
    path.write_text(
      "".join(
        r + "\n"
        for r in (
          row(0, "HV_W", -12250),
          row(0, "HV_T_3", 21),
          row(0, "TYRE_P_FL", 38),
          row(0, "IGNITION", 1),
          row(0, "NEW_THING", 2),
        )
      )
    )
    trip, ms = "2026-05-24 20-10-53", START * 1000
    self.assertEqual(
      self.converted(path).splitlines(),
      [
        f'car_bms_battery_power{{unit="kW",trip="{trip}",car="ev6"}} -12.25 {ms}',
        f'car_bms_battery_pack_b03{{unit="°C",trip="{trip}",car="ev6"}} 21.0 {ms}',
        f'car_tyre_p_fl{{unit="psi",trip="{trip}",car="ev6"}} 38.0 {ms}',
        f'car_ignition{{unit="unknown",trip="{trip}",car="ev6"}} 1.0 {ms}',
        f'car_new_thing{{unit="unknown",trip="{trip}",car="ev6"}} 2.0 {ms}',
      ],
    )

  def test_splits_trips_on_gaps_and_continues_them_across_batches(self):
    # 10 min apart continues; 10 min and 1 s apart starts a new trip
    self.batch(name(EV6, 1), [row(0, "SOC", 80), row(600, "SOC", 79), row(1201, "SOC", 78)])
    self.batch(name(EV6, 2), [row(1500, "SOC", 77)])
    self.ingest(DAY_LATER)
    self.assertEqual(
      self.trips_and_values(),
      [
        "2026-05-24 20-10-53 80.0",
        "2026-05-24 20-10-53 79.0",
        "2026-05-24 20-30-54 78.0",
        "2026-05-24 20-30-54 77.0",
      ],
    )

  def test_labels_each_dongle_as_its_own_car(self):
    self.upload("trip.brc", *TRIP)
    # Interleaved in time: neither dongle's batches continue the other's trip
    self.batch(name(EV6, 1), [row(0, "SOC", 80)])
    self.batch(name(OTHER, 1), [row(120, "SOC", 50)])
    self.batch(name(EV6, 2), [row(240, "SOC", 79)])
    self.batch(name(OTHER, 2), [row(600, "SOC", 49)])
    _, err = self.ingest(DAY_LATER)
    self.assertIn(f"dongle {OTHER} isn't in the car sources", err)
    pattern = r'^([a-z_0-9]+)\{[^}]*trip="([^"]*)",car="([^"]*)"\} ([0-9.-]+) [0-9]+$'
    self.assertEqual(
      [re.sub(pattern, r"\1 \3 \2 \4", line) for line in self.posted_lines()],
      [
        "car_bms_battery_power ev6 trip -12.25",
        "car_bms_battery_power ev6 trip 4.5",
        "car_bms_state_of_charge_bms ev6 2026-05-24 20-10-53 80.0",
        "car_bms_state_of_charge_bms ev6 2026-05-24 20-10-53 79.0",
        f"car_bms_state_of_charge_bms {OTHER} 2026-05-24 20-12-53 50.0",
        f"car_bms_state_of_charge_bms {OTHER} 2026-05-24 20-12-53 49.0",
      ],
    )

  def test_imports_a_batch_once(self):
    self.batch(name(EV6, 1), [row(0, "SOC", 80)])
    self.ingest(DAY_LATER)
    (self.car / name(EV6, 9)).write_bytes((self.car / name(EV6, 1)).read_bytes())
    self.settle(self.car / name(EV6, 9))
    self.ingest(DAY_LATER + 900)
    self.assertEqual(len(self.posted_lines()), 1)
    self.assertEqual(self.state(), [f"ok\t1\t{name(EV6, 1)}"])

  def test_orders_batches_by_cursor_offset_as_a_number(self):
    # By name, offset 10000 sorts before 9000 and would start a second trip at 16 min
    self.batch(name(EV6, 1, 0), [row(0, "SOC", 80)])
    self.batch(name(EV6, 1, 9000), [row(480, "SOC", 79)])
    self.batch(name(EV6, 1, 10000), [row(960, "SOC", 78)])
    self.ingest(DAY_LATER)
    self.assertEqual(
      self.trips_and_values(),
      ["2026-05-24 20-10-53 80.0", "2026-05-24 20-10-53 79.0", "2026-05-24 20-10-53 78.0"],
    )

  # Configuration

  def test_reads_its_config_from_flags_over_the_environment(self):
    env = {
      "TZ": "Europe/Berlin",
      "CAR_INGEST_VM_URL": "https://vm.example",
      "CAR_INGEST_DEFAULT_CAR": "kona",
      "CAR_INGEST_CAR_SOURCES": "a=ev6, b=kona",
      "CAR_INGEST_RETENTION_DAYS": "30",
    }
    cfg, args = car_ingest.parse_args(["--default-car", "ioniq", "--car-dir", "/uploads"], env)
    self.assertEqual(
      (cfg.car_dir, cfg.default_car, cfg.retention_days, cfg.car_sources, cfg.timezone.key),
      (Path("/uploads"), "ioniq", 30, {"a": "ev6", "b": "kona"}, "Europe/Berlin"),
    )
    self.assertEqual(args.vm_url, "https://vm.example")

  def test_rejects_bad_config(self):
    for argv, env in (
      (["--timezone", "Mars/Olympus"], {"CAR_INGEST_VM_URL": "x"}),
      (["--car-sources", "nocar"], {"CAR_INGEST_VM_URL": "x"}),
      ([], {}),
    ):
      with (
        self.subTest(argv=argv, env=env),
        contextlib.redirect_stderr(io.StringIO()),
        self.assertRaises(SystemExit),
      ):
        car_ingest.parse_args(argv, env)


if __name__ == "__main__":
  unittest.main()
