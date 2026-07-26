# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for the bundled in-process time handler."""

from __future__ import annotations

import pytest

from thingctx.contrib.time import TimeHandler


def test_get_current_time_rejects_unknown_timezone():
    handler = TimeHandler()

    with pytest.raises(ValueError, match="unknown IANA timezone"):
        handler.getCurrentTime("Not/A_Real_Zone")


def test_convert_time_rejects_unrecognized_time():
    handler = TimeHandler()

    with pytest.raises(ValueError, match="unrecognized time"):
        handler.convertTime(
            "not-a-time",
            source_timezone="UTC",
            target_timezone="Asia/Tokyo",
        )


@pytest.mark.parametrize(
    ("value", "expected_source", "expected_target"),
    [
        (
            "2026-01-15T09:00:00",
            "2026-01-15T09:00:00+00:00",
            "2026-01-15T18:00:00+09:00",
        ),
        (
            "2026-01-15T09:00:00+02:00",
            "2026-01-15T09:00:00+02:00",
            "2026-01-15T16:00:00+09:00",
        ),
    ],
)
def test_convert_time_accepts_iso_8601(
    value,
    expected_source,
    expected_target,
):
    handler = TimeHandler()

    result = handler.convertTime(
        value,
        source_timezone="UTC",
        target_timezone="Asia/Tokyo",
    )

    assert result["source"]["datetime"] == expected_source
    assert result["target"]["datetime"] == expected_target
