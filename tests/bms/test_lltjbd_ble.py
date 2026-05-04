# -*- coding: utf-8 -*-
"""Tests for LltJbd_Ble — focused on the back-off / reconnect behaviour.

These tests verify that a transient BLE failure doesn't kill the driver
(and require a supervisor restart). The previous behaviour set
``self.run = False`` on every BleakScanner / BleakClient exception, which
made ``background_loop()`` exit. The new behaviour uses an exponential
back-off so the loop can keep retrying.
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

# bleak is not installed in CI/dev venvs; stub before importing the driver.
_bleak_exc = types.ModuleType("bleak.exc")
_bleak_exc.BleakDBusError = type("BleakDBusError", (Exception,), {})
sys.modules.setdefault("bleak.exc", _bleak_exc)


class _BleakScannerStub:
    """Class-shaped stub so ``patch.object`` finds find_device_by_address."""

    @staticmethod
    async def find_device_by_address(*args, **kwargs):
        return None


_bleak = types.ModuleType("bleak")
_bleak.BleakClient = MagicMock
_bleak.BleakScanner = _BleakScannerStub
_bleak.BLEDevice = MagicMock
sys.modules.setdefault("bleak", _bleak)

# bms.lltjbd is the parent class — also drags in serial bits via parents.
# The conftest.py at tests/conftest.py already stubs serial and dbus.

import os  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "dbus-serialbattery"))

from bms.lltjbd_ble import (  # noqa: E402
    LltJbd_Ble,
    RECONNECT_BACKOFF_INITIAL_S,
    RECONNECT_BACKOFF_MAX_S,
)


def _make_driver():
    """Construct a LltJbd_Ble without running __init__'s hciattach probe.

    __init__ touches /tmp files and the hciattach process table; for unit
    tests we bypass it and set the few attributes that the methods under
    test actually read.
    """
    drv = LltJbd_Ble.__new__(LltJbd_Ble)
    drv.address = "AA:BB:CC:DD:EE:FF"
    drv.run = True
    drv.bt_loop = None
    drv.bt_client = None
    drv.device = None
    drv.response_queue = None
    drv.ready_event = asyncio.Event()
    drv.hci_uart_ok = True
    drv.reconnect_backoff_s = RECONNECT_BACKOFF_INITIAL_S
    drv.main_thread = MagicMock()
    drv.main_thread.is_alive.return_value = True
    return drv


# ---------- back-off mechanics ----------


def test_reconnect_backoff_doubles_each_call():
    """Each call to _reconnect_backoff doubles the delay until the cap."""
    drv = _make_driver()
    drv.reconnect_backoff_s = 5

    with patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
        asyncio.run(drv._reconnect_backoff())
    assert drv.reconnect_backoff_s == 10

    with patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
        asyncio.run(drv._reconnect_backoff())
    assert drv.reconnect_backoff_s == 20


def test_reconnect_backoff_caps_at_max():
    """The back-off must never exceed RECONNECT_BACKOFF_MAX_S."""
    drv = _make_driver()
    drv.reconnect_backoff_s = RECONNECT_BACKOFF_MAX_S

    with patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
        asyncio.run(drv._reconnect_backoff())

    assert drv.reconnect_backoff_s == RECONNECT_BACKOFF_MAX_S


def test_reconnect_backoff_actually_sleeps_for_current_delay():
    """The current back-off value drives the sleep duration."""
    drv = _make_driver()
    drv.reconnect_backoff_s = 7

    sleep_mock = AsyncMock()
    with patch("bms.lltjbd_ble.asyncio.sleep", sleep_mock):
        asyncio.run(drv._reconnect_backoff())

    sleep_mock.assert_awaited_once_with(7)


# ---------- bt_main_loop survival behaviour ----------


def test_scanner_returns_no_device_does_not_kill_driver():
    """Previously: scanner returns None -> self.run = False; supervisor
    restart needed. Now: scanner returns None -> back-off + return, run
    stays True, background_loop() will retry."""
    drv = _make_driver()

    async def _scan(*args, **kwargs):
        return None  # device not found

    with patch("bms.lltjbd_ble.BleakScanner.find_device_by_address", new=_scan), \
         patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
        asyncio.run(drv.bt_main_loop())

    assert drv.run is True
    assert drv.device is None


def test_bleak_client_timeout_does_not_kill_driver():
    """asyncio.TimeoutError from BleakClient must trigger back-off, not
    a supervisor-requiring shutdown."""
    drv = _make_driver()

    async def _scan(*args, **kwargs):
        return MagicMock()  # device found

    class _RaisingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise asyncio.exceptions.TimeoutError()

        async def __aexit__(self, *args):
            return False

    with patch("bms.lltjbd_ble.BleakScanner.find_device_by_address", new=_scan), \
         patch("bms.lltjbd_ble.BleakClient", _RaisingClient), \
         patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
        asyncio.run(drv.bt_main_loop())

    assert drv.run is True
    assert drv.bt_loop is None  # cleaned up


def test_bleak_client_generic_exception_does_not_kill_driver():
    """Any other exception from BleakClient also goes through back-off."""
    drv = _make_driver()

    async def _scan(*args, **kwargs):
        return MagicMock()

    class _RaisingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("BlueZ DBus glitch")

        async def __aexit__(self, *args):
            return False

    with patch("bms.lltjbd_ble.BleakScanner.find_device_by_address", new=_scan), \
         patch("bms.lltjbd_ble.BleakClient", _RaisingClient), \
         patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
        asyncio.run(drv.bt_main_loop())

    assert drv.run is True


def test_repeated_failures_grow_backoff():
    """Three consecutive failures increase the back-off three times."""
    drv = _make_driver()
    initial = drv.reconnect_backoff_s

    async def _scan(*args, **kwargs):
        return None

    for _ in range(3):
        with patch("bms.lltjbd_ble.BleakScanner.find_device_by_address", new=_scan), \
             patch("bms.lltjbd_ble.asyncio.sleep", new=AsyncMock()):
            asyncio.run(drv.bt_main_loop())

    assert drv.reconnect_backoff_s == min(initial * 8, RECONNECT_BACKOFF_MAX_S)
    assert drv.run is True


# ---------- on_disconnect cleanup ----------


def test_on_disconnect_clears_bt_loop():
    """Bleak's disconnect callback must invalidate bt_loop so concurrent
    read_serial_data_llt() calls bail out cleanly on the existing
    `if not self.bt_loop: return False` guard."""
    drv = _make_driver()
    drv.bt_loop = MagicMock()  # pretend a loop is active

    drv.on_disconnect(MagicMock())  # bleak passes the client, we ignore it

    assert drv.bt_loop is None


def test_read_serial_data_llt_short_circuits_after_disconnect():
    """Smoke-test the integration with the existing guard: after
    on_disconnect() clears bt_loop, read_serial_data_llt() returns False
    without touching asyncio/Bleak."""
    drv = _make_driver()
    drv.bt_loop = MagicMock()
    drv.on_disconnect(MagicMock())

    assert drv.read_serial_data_llt(b"\xdd\xa5\x03\x00\xff\xfd\x77") is False
