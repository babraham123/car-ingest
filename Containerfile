# car_ingest's zipapp on Python's standard library. The base image carries the tzdata
# zoneinfo reads the phone's time zone from, and the CA certificates for an HTTPS
# VictoriaMetrics.
FROM docker.io/library/python:3.14-alpine AS build
COPY build.py /build/
COPY src /build/src
RUN python3 /build/build.py /car_ingest

FROM docker.io/library/python:3.14-alpine
COPY --from=build /car_ingest /usr/local/bin/car_ingest
ENTRYPOINT ["python3", "/usr/local/bin/car_ingest"]
