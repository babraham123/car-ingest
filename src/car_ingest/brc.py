# Car Scanner recordings (.brc), decoded in docs/brc-format.md: .NET BinaryWriter output,
# little-endian, strings 7-bit-varint length-prefixed UTF-8. A header, then sensor
# definitions and samples interleaved, each behind a marker. Anything else fails the file.

import datetime
import math
import struct

from .util import FormatError, slug

MAGIC = "CARSCANNERRECORD"
DEF_MARKER = 0x00D56190
SAMPLE_MARKER = 0x01274350
# Car Scanner's unit enum; values are SI whatever the app displays.
UNITS = {
  0: "none",
  3: "km",
  6: "rpm",
  10: "V",
  14: "%",
  15: "°C",
  17: "s",
  18: "min",
  27: "kOhm",
  30: "Hz",
  46: "kW",
  54: "h",
  58: "currency",
  59: "A",
  60: "kWh",
  61: "Wh",
  62: "Ah",
  63: "hours",
}
DOTNET_EPOCH = datetime.datetime(1, 1, 1)


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
