#!/usr/bin/env python3
"""Logging for the WDA API: every action to stdout, so `docker logs` has it.

Format (ISO 8601 with offset, so a log line is unambiguous across timezones and
sorts correctly):

    2026-09-01T10:14:07+02:00 INFO  wda.http 192.168.2.9 admin POST
        /wda/methods/0-0-firmwareupdate-activate/runs 201 3ms

Levels are chosen so a running device produces a readable log rather than a
firehose:
  INFO   every WDA parameter read, method run, file-upload summary, and every
         update state transition
  DEBUG  /health (a 30s healthcheck is 2880 lines a day) and each individual
         upload chunk (a 1.3 GB bundle is over a thousand of them)

WDA_LOG_LEVEL=DEBUG turns those on when you are actually debugging.

Never logged: the Authorization header, the password, or any request body. The
username is logged - knowing who invoked a firmware update is the point.
"""
import logging
import os
import sys
import time

LEVEL = os.environ.get("WDA_LOG_LEVEL", "INFO").upper()


class _Formatter(logging.Formatter):
    """ISO 8601 local time with UTC offset, e.g. 2026-09-01T10:14:07+02:00."""
    def formatTime(self, record, datefmt=None):
        t = time.localtime(record.created)
        return time.strftime("%Y-%m-%dT%H:%M:%S", t) + time.strftime("%z", t)[:3] \
            + ":" + time.strftime("%z", t)[3:]


def setup():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, LEVEL, logging.INFO))
    return root


http = logging.getLogger("wda.http")      # one line per request
method = logging.getLogger("wda.method")  # one line per method invocation
update = logging.getLogger("wda.update")  # firmware-update state transitions
