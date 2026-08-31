# Unprivileged rauc client. The host rauc.service does all privileged work
# (writing the inactive A/B slot, updating grubenv); this container just sends
# it the install request over the host system D-Bus socket.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rauc dbus \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
