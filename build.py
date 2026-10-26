#!/usr/bin/env python3
# Builds dist/car_ingest: the package in src/ as one executable zipapp, which the
# Containerfile installs as /usr/local/bin/car_ingest.
# Usage:
#   python3 build.py [OUTPUT]

import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build(output=ROOT / "dist" / "car_ingest"):
  output = Path(output)
  output.parent.mkdir(parents=True, exist_ok=True)
  zipapp.create_archive(
    ROOT / "src",
    output,
    interpreter="/usr/bin/env python3",
    main="car_ingest.cli:entry",
    filter=lambda path: "__pycache__" not in path.parts,
  )
  return output


if __name__ == "__main__":
  print(build(*sys.argv[1:]))
