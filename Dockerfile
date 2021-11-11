FROM quay.io/edge-infrastructure/assisted-service:latest AS service

FROM quay.io/centos/centos:stream8
RUN dnf update -y && dnf install -y python3 python3-pip && dnf clean all && python3 -m pip install --upgrade pip
WORKDIR assisted_event_scrape/

COPY requirements.txt .
RUN python3 -m pip install -I --no-cache-dir -r requirements.txt vcversioner

COPY .  .
RUN python3 -m pip install .

ENTRYPOINT ["events_scrape"]
