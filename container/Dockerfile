# Self-contained edge updater: carries the .raucb inside the image. Deploy on
# any of our edges with just the D-Bus socket mounted - no bundle to pre-place.
# The host rauc.service does the privileged slot write; this stays unprivileged.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rauc dbus python3 \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
COPY api.py /api.py
COPY bundle.raucb /firmware/bundle.raucb
RUN chmod +x /entrypoint.sh
ENV BUNDLE=/firmware/bundle.raucb
# MODE=server exposes the REST API on this port; oneshot mode ignores it.
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
