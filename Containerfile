# car_ingest.py on Python's standard library. The base image carries the tzdata zoneinfo
# reads the phone's time zone from, and the CA certificates for an HTTPS VictoriaMetrics.
FROM docker.io/library/python:3.14-alpine
ENV PYTHONDONTWRITEBYTECODE=1
COPY car_ingest.py /usr/local/bin/car_ingest.py
ENTRYPOINT ["python3", "/usr/local/bin/car_ingest.py"]
