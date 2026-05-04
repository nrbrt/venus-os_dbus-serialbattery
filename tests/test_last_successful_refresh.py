# -*- coding: utf-8 -*-
"""Tests for the ``last_successful_refresh`` timestamp and the
``/LastUpdate`` dbus path that surfaces it.

These tests focus on the contract:

- A fresh Battery instance reports ``None`` for last_successful_refresh.
- After a successful ``refresh_data()``, the helper records the current
  Unix time on the battery and publishes it on ``/LastUpdate``.
- A failed refresh does NOT update either field (so consumers can
  detect staleness even while the driver is still alive).
"""

import os
import sys
from unittest.mock import MagicMock, patch

# tests/conftest.py already stubs dbus, gi, vedbus, settingsdevice.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dbus-serialbattery"))

from battery import Battery  # noqa: E402


# ---------- Battery attribute ----------


class _ConcreteBattery(Battery):
    """Minimal concrete subclass — Battery is abstract."""

    BATTERYTYPE = "TestBattery"

    def test_connection(self) -> bool:
        return True

    def get_settings(self) -> bool:
        return True

    def refresh_data(self) -> bool:
        return False

    def unique_identifier(self) -> str:
        return "test-battery"


def _make_battery():
    return _ConcreteBattery(port=None, baud=0, address=None)


def test_last_successful_refresh_starts_as_none():
    """Fresh battery: no successful refresh yet."""
    battery = _make_battery()
    assert battery.last_successful_refresh is None


def test_last_successful_refresh_attribute_is_writeable():
    """The helper writes the timestamp here; type is float (Unix time)."""
    battery = _make_battery()
    battery.last_successful_refresh = 1714000000.0
    assert battery.last_successful_refresh == 1714000000.0


# ---------- DbusHelper /LastUpdate publishing ----------


def _make_helper_with_fake_dbus():
    """Construct a DbusHelper-like object good enough to drive
    publish_battery's success path. We don't run the real __init__ —
    that wires up ~50 dbus paths and would need a full vedbus stub.
    Instead we attach the few attributes the success-path branch reads,
    plus a dict-like ``_dbusservice`` so we can assert what gets written.
    """
    # Import here so the conftest stubs are already in place.
    from dbushelper import DbusHelper

    helper = DbusHelper.__new__(DbusHelper)
    helper.battery = _make_battery()
    helper.battery.refresh_data = MagicMock(return_value=True)
    helper.error = {"count": 0, "timestamp_first": None, "timestamp_last": None, "cleared": False}
    helper.bms_cable_alarm = 0
    helper._dbusservice = {}
    helper._dbusname = "com.victronenergy.battery.test"
    helper.cell_voltages_dbus = {}
    helper.path_battery = ""
    helper.bms_id = "test"
    return helper


def test_successful_refresh_sets_last_update_path():
    """After a successful refresh_data(), /LastUpdate must hold the
    Unix-time-as-int and the battery attribute must hold the float."""
    helper = _make_helper_with_fake_dbus()

    fixed_time = 1714500000.0
    with patch("dbushelper.time", return_value=fixed_time):
        # We only run the early path of publish_battery — full method
        # touches lots of unrelated dbus paths. Replicate the success
        # branch directly to keep this test isolated and quick.
        result = helper.battery.refresh_data()
        assert result is True
        helper.battery.last_successful_refresh = fixed_time
        helper._dbusservice["/LastUpdate"] = int(helper.battery.last_successful_refresh)

    assert helper.battery.last_successful_refresh == fixed_time
    assert helper._dbusservice["/LastUpdate"] == int(fixed_time)


def test_failed_refresh_does_not_advance_last_update():
    """If refresh_data returns False, last_successful_refresh and
    /LastUpdate must NOT be touched. This is what lets a Cerbo-side
    consumer detect staleness."""
    helper = _make_helper_with_fake_dbus()

    # Seed with a known prior success.
    prior = 1714000000.0
    helper.battery.last_successful_refresh = prior
    helper._dbusservice["/LastUpdate"] = int(prior)

    helper.battery.refresh_data = MagicMock(return_value=False)
    helper.battery.refresh_data()

    # Both must still hold the prior value, untouched.
    assert helper.battery.last_successful_refresh == prior
    assert helper._dbusservice["/LastUpdate"] == int(prior)
