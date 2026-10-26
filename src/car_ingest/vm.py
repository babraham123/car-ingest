# VictoriaMetrics' import format and endpoint.

import base64
import time
import urllib.error
import urllib.request

IMPORT_PATH = "/api/v1/import/prometheus"


def import_lines(samples):
  """VictoriaMetrics' import format: Prometheus text with millisecond timestamps."""
  return "".join(f"{series} {value!r} {ms}\n" for series, value, ms in samples)


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
