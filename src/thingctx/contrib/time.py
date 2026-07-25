# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""In-process time handler for the registry ``time`` Thing.

Bound via the ``thingctx.local_handlers`` / ``time`` entry point. Pure
functions over the standard library; no connector, no configuration. Method
names match the TD forms: ``getCurrentTime``, ``convertTime``.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# IANA name accepted by getCurrentTime when the caller omits one.
_DEFAULT_TZ = "UTC"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc


class TimeHandler:
    """In-process handler for ``urn:thingctx:time``."""

    def getCurrentTime(self, timezone: str = _DEFAULT_TZ) -> dict:  # noqa: N802 - matches TD action name
        tz = _zone(timezone or _DEFAULT_TZ)
        now = datetime.now(tz)
        return {
            "timezone": timezone or _DEFAULT_TZ,
            "datetime": now.isoformat(),
            "utc_offset": now.strftime("%z"),
        }

    def convertTime(self, time: str, source_timezone: str, target_timezone: str) -> dict:  # noqa: N802
        src = _zone(source_timezone)
        dst = _zone(target_timezone)
        # Accept a bare wall-clock time (HH:MM[:SS]) against today, or a full ISO 8601 stamp.
        parsed = self._parse(time, src)
        converted = parsed.astimezone(dst)
        return {
            "source": {"timezone": source_timezone, "datetime": parsed.isoformat()},
            "target": {"timezone": target_timezone, "datetime": converted.isoformat()},
        }

    @staticmethod
    def _parse(value: str, tz: ZoneInfo) -> datetime:
        raw = value.strip()
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            for fmt in ("%H:%M:%S", "%H:%M"):
                try:
                    t = datetime.strptime(raw, fmt).time()
                except ValueError:
                    continue
                return datetime.now(tz).replace(
                    hour=t.hour, minute=t.minute, second=t.second, microsecond=0
                )
            raise ValueError(f"unrecognized time {value!r}: use ISO 8601 or HH:MM[:SS]") from None
        return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def make_time_handler() -> TimeHandler:
    """Entry-point factory for ``thingctx.local_handlers``."""
    return TimeHandler()
