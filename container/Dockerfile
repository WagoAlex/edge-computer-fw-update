# Self-contained edge updater: carries the .raucb inside the image. Deploy on
# any of our edges with just the D-Bus socket mounted - no bundle to pre-place.
# The host rauc.service does the privileged slot write; this stays unprivileged.
FROM debian:trixie-slim AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends rauc dbus python3 openssl iproute2 \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
COPY api.py openapi.py presets.py wdalog.py /
COPY providers/ /providers/
COPY presets/ /presets/
RUN chmod +x /entrypoint.sh
WORKDIR /
ENV BUNDLE=/firmware/bundle.raucb PYTHONPATH=/ PYTHONUNBUFFERED=1
# MODE=server exposes the REST API on this port; oneshot mode ignores it.
EXPOSE 8443
ENTRYPOINT ["/entrypoint.sh"]

# --- dev: same image plus pytest, for `docker build --target dev` -------------
FROM base AS dev
RUN apt-get update && apt-get install -y --no-install-recommends python3-pytest \
 && rm -rf /var/lib/apt/lists/*
COPY tests/ /tests/
WORKDIR /

# --- default: production image, bundle baked in -------------------------------
FROM base AS prod
COPY bundle.raucb /firmware/bundle.raucb
