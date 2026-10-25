# car-ingest

Imports car telemetry files into [VictoriaMetrics](https://victoriametrics.com/) at the samples'
original timestamps:

- Car Scanner Pro recordings (`.brc`), format in [docs/brc-format.md](docs/brc-format.md)
- WiCAN Pro logger batches (`.jsonl`, `.jsonl.gz`), format in
  [docs/wican-jsonl.md](docs/wican-jsonl.md)

One run scans a directory of uploads, imports every file it hasn't seen, and exits; schedule it
with a timer or cron. It is one Python script on the standard library only.

## Behaviour

- Each file is imported once, keyed by its sha256 in the state file, so a re-upload adds nothing.
- Files modified in the last minute are left for the next run, since they may still be uploading.
- A malformed file moves to `failed/` in the upload directory.
- A file whose oldest sample is older than `--retention-days` stays in place and is recorded as
  `too_old`: VictoriaMetrics would silently drop its samples.
- A failed import stops the run without recording the file, so the next run retries it.
- The files are never deleted; they are the long-term archive and can be re-imported with a fresh
  state directory.

Every series is `car_<sensor>{unit="…",trip="…",car="…"}`. The WiCAN params a Car Scanner
dashboard uses map onto the Car Scanner series names, so one dashboard serves both sources.

After each run it writes Prometheus textfile metrics for node_exporter's textfile collector:

```text
car_ingest_files_total{result="ok|failed|too_old"}
car_ingest_last_success_timestamp_seconds
```

## Configuration

Every option has an environment variable; a flag wins over its variable.

| Flag | Variable | Default | Meaning |
| --- | --- | --- | --- |
| `--vm-url` | `CAR_INGEST_VM_URL` | required | VictoriaMetrics base URL; posts to `/api/v1/import/prometheus` |
| `--vm-user` | `CAR_INGEST_VM_USER` | `admin` | Basic auth user |
| | `CAR_INGEST_VM_PASSWORD` | unset | Basic auth password; no auth when unset |
| `--car-dir` | `CAR_INGEST_CAR_DIR` | `/car` | Upload directory |
| `--state-dir` | `CAR_INGEST_STATE_DIR` | `/state` | Holds `state` (imported files) and `trips` |
| `--metrics-file` | `CAR_INGEST_METRICS_FILE` | `/metrics/car_ingest.prom` | Textfile metrics |
| `--timezone` | `CAR_INGEST_TIMEZONE`, then `TZ` | `UTC` | The phone's zone: `.brc` start times are local, and trips are named in it |
| `--retention-days` | `CAR_INGEST_RETENTION_DAYS` | `31` | VictoriaMetrics' `-retentionPeriod` in days |
| `--default-car` | `CAR_INGEST_DEFAULT_CAR` | `car` | `car` label for `.brc` files and batches not named by the sync app |
| `--car-sources` | `CAR_INGEST_CAR_SOURCES` | empty | `SOURCE=CAR,…`: each WiCAN dongle id's car label |

The password has no flag, so it never shows in a process list.

## Usage

```bash
# One run over the upload directory
CAR_INGEST_VM_PASSWORD=… ./car_ingest.py --vm-url https://metrics.example.com \
  --car-dir ./uploads --state-dir ./state --metrics-file ./car_ingest.prom \
  --timezone America/Los_Angeles --default-car ev6 --car-sources wican_AA_BB_CC_DD_EE_01=ev6

# Print one file's import lines, or why it fails, without importing
./car_ingest.py --timezone America/Los_Angeles 'uploads/2026-05-24 20-10-53.brc'
```

### Container

```bash
podman build -t car_ingest -f Containerfile .
podman run --rm \
  -v /path/to/uploads:/car -v /var/lib/car_ingest:/state \
  -v /var/lib/node_exporter/textfile_collector:/metrics \
  -e TZ=America/Los_Angeles -e CAR_INGEST_VM_URL=https://metrics.example.com \
  -e CAR_INGEST_VM_PASSWORD=… -e CAR_INGEST_DEFAULT_CAR=ev6 \
  car_ingest
```

The container runs as root by default; it needs write access to the upload directory (for
`failed/`), the state directory and the metrics directory.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests write synthetic `.brc` recordings and WiCAN batches and cover the conversions, param
mapping, trips, the `car` label, configuration, and a run's import-once, `failed/`, `too_old` and
retry handling.

## License

MIT, see [LICENSE](LICENSE).
