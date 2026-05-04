# -*- coding: utf-8 -*-

# Notes
# Updated by https://github.com/idstein

import asyncio
import atexit
import functools
import os
import shlex
import subprocess
import threading
import sys
import re
from asyncio import CancelledError
from time import time
from typing import Union, Optional
from utils import get_connection_error_message, logger, BLUETOOTH_FORCE_RESET_BLE_STACK
from utils_ble import restart_ble_hardware_and_bluez_driver
from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakDBusError
from bms.lltjbd import LltJbdProtection, LltJbd

BLE_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
BLE_CHARACTERISTICS_TX_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
BLE_CHARACTERISTICS_RX_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
MIN_RESPONSE_SIZE = 6
MAX_RESPONSE_SIZE = 256

# Reconnect back-off when BleakScanner / BleakClient raises a transient
# error. Previously the driver set self.run = False and required a
# supervisor restart; now bt_main_loop() returns and background_loop()
# re-enters after this back-off.
RECONNECT_BACKOFF_INITIAL_S = 5
RECONNECT_BACKOFF_MAX_S = 60

# BLE-level heartbeat: if no bytes are received from the BMS for this many
# seconds while the BleakClient still claims to be connected, force a
# reconnect. Catches "silently dead" links — BlueZ keeps the connection
# object alive but no data flows. Tuned so it's longer than the BMS
# poll_interval (1000 ms by default) plus several full status reads, but
# short enough that a stuck link is detected within one Cerbo update window.
BLE_HEARTBEAT_TIMEOUT_S = 15


class LltJbd_Ble(LltJbd):
    BATTERYTYPE = "LLT/JBD BLE"

    def __init__(self, port: Optional[str], baud: Optional[int], address: str):
        super(LltJbd_Ble, self).__init__(port, -1, address)

        self.address = address
        self.protection = LltJbdProtection()
        self.type = self.BATTERYTYPE
        self.main_thread = threading.current_thread()
        self.data: bytearray = bytearray()
        self.run = True
        self.bt_thread = threading.Thread(name="LltJbd_Ble_Loop", target=self.background_loop, daemon=True)
        self.bt_loop: Optional[asyncio.AbstractEventLoop] = None
        self.bt_client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self.response_queue: Optional[asyncio.Queue] = None
        self.ready_event: Optional[asyncio.Event] = None

        self.reconnect_backoff_s = RECONNECT_BACKOFF_INITIAL_S
        # Heartbeat timestamp — updated on every successful BLE byte
        # arrival in send_command(). Initialised to construction time so
        # the staleness check doesn't false-fire before the first read.
        self.last_ble_data_received: float = time()

        self.hci_uart_ok = True
        if not os.path.isfile("/tmp/dbus-blebattery-hciattach"):
            execfile = open("/tmp/dbus-blebattery-hciattach", "w")
            execpath = os.popen("ps -ww | grep hciattach | grep -v grep").read()
            execpath = re.search("/usr/bin/hciattach.+", execpath)
            execfile.write(execpath.group())
            execfile.close()
        else:
            execpath = os.popen("ps -ww | grep hciattach | grep -v grep").read()
            if not execpath:
                execfile = open("/tmp/dbus-blebattery-hciattach", "r")
                os.system(execfile.readline())
                execfile.close()

        logger.info("Init of LltJbd_Ble at " + address)

    def connection_name(self) -> str:
        return "BLE " + self.address

    def custom_name(self) -> str:
        return self.device.name

    def on_disconnect(self, client):
        """Bleak invokes this when the peripheral drops the link.

        Previously the callback only logged. We now also clear ``bt_loop``
        so concurrent ``read_serial_data_llt()`` calls bail out
        immediately (they short-circuit on ``if not self.bt_loop`` at
        the top) instead of trying to dispatch a coroutine onto a now-
        dead event loop. ``bt_main_loop()``'s ``async with BleakClient``
        block still does the actual reconnection work.
        """
        logger.warning("BLE client disconnected — invalidating bt_loop")
        self.bt_loop = None

    async def bt_main_loop(self):
        logger.info("|- Try to connect to LltJbd_Ble at " + self.address)
        try:
            self.device = await BleakScanner.find_device_by_address(self.address, cb=dict(use_bdaddr=True))

        except Exception:
            exception_type, exception_object, exception_traceback = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            if "Bluetooth adapters" in repr(exception_object):
                await self.reset_hci_uart()
            else:
                logger.error(f"BleakScanner(): Exception occurred: {repr(exception_object)} of type {exception_type} " f"in {file} line #{line}")

            self.device = None
            await asyncio.sleep(0.5)
            # allow the bluetooth connection to recover
            await asyncio.sleep(5)

        if not self.device:
            # Don't kill the driver — background_loop() will retry after a
            # back-off so a temporary scanner failure (BlueZ hiccup, BMS
            # asleep) doesn't require a supervisor restart.
            await self._reconnect_backoff()
            return

        try:
            async with BleakClient(self.device, disconnected_callback=self.on_disconnect) as client:
                self.bt_client = client
                logger.info("|- Device connected, check if it's really a LLT/JBD BMS")
                self.bt_loop = asyncio.get_event_loop()
                self.response_queue = asyncio.Queue()
                self.ready_event.set()
                # Successful connection — reset back-off and heartbeat so
                # the next dropout starts retrying quickly again.
                self.reconnect_backoff_s = RECONNECT_BACKOFF_INITIAL_S
                self.last_ble_data_received = time()
                while self.run and client.is_connected and self.main_thread.is_alive():
                    if self._ble_data_is_stale(time()):
                        logger.warning(f"BLE heartbeat: no data for >{BLE_HEARTBEAT_TIMEOUT_S}s, forcing reconnect")
                        break
                    await asyncio.sleep(0.1)
            self.bt_loop = None

        # Exception occurred: TimeoutError() of type <class 'asyncio.exceptions.TimeoutError'>
        except asyncio.exceptions.TimeoutError:
            exception_type, exception_object, exception_traceback = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"BleakClient(): asyncio.exceptions.TimeoutError: {repr(exception_object)} of type {exception_type} " f"in {file} line #{line}")
            self.bt_loop = None
            await self._reconnect_backoff()
            return

        except TimeoutError:
            exception_type, exception_object, exception_traceback = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"BleakClient(): TimeoutError: {repr(exception_object)} of type {exception_type} " f"in {file} line #{line}")
            self.bt_loop = None
            await self._reconnect_backoff()
            return

        except Exception:
            exception_type, exception_object, exception_traceback = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"BleakClient(): Exception occurred: {repr(exception_object)} of type {exception_type} " f"in {file} line #{line}")
            self.bt_loop = None
            await self._reconnect_backoff()
            return

    async def _reconnect_backoff(self) -> None:
        """Sleep for the current back-off and double it for next time, capped
        at RECONNECT_BACKOFF_MAX_S. Resets to RECONNECT_BACKOFF_INITIAL_S after
        a successful connection so a single hiccup doesn't permanently slow
        down recovery."""
        delay = self.reconnect_backoff_s
        logger.info(f"|- Reconnect back-off: {delay}s before next attempt")
        await asyncio.sleep(delay)
        self.reconnect_backoff_s = min(delay * 2, RECONNECT_BACKOFF_MAX_S)

    def _ble_data_is_stale(self, now: float) -> bool:
        """Return True if no BLE data has been received from the BMS for
        longer than ``BLE_HEARTBEAT_TIMEOUT_S``. Used by ``bt_main_loop``
        to detect 'silently dead' connections — BlueZ keeps the link
        object alive but no bytes flow, which the existing
        ``client.is_connected`` check doesn't catch."""
        return (now - self.last_ble_data_received) > BLE_HEARTBEAT_TIMEOUT_S

    def background_loop(self):
        while self.run and self.main_thread.is_alive():
            asyncio.run(self.bt_main_loop())

    async def async_test_connection(self):
        if self.hci_uart_ok:
            self.ready_event = asyncio.Event()
            if not self.bt_thread.is_alive():
                self.bt_thread.start()

                def shutdown_ble_atexit(thread):
                    self.run = False
                    thread.join()

                atexit.register(shutdown_ble_atexit, self.bt_thread)
            try:
                return await asyncio.wait_for(self.ready_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.error(">>> ERROR: Unable to connect with BLE device")
                return False
        else:
            return False

    def test_connection(self):
        # call a function that will connect to the battery, send a command and retrieve the result.
        # The result or call should be unique to this BMS. Battery name or version, etc.
        # Return True if success, False for failure
        result = False
        try:
            if self.address:
                result = True
            if result and asyncio.run(self.async_test_connection()):
                result = True
            if result:
                result = super().test_connection()
        except Exception:
            exception_type, exception_object, exception_traceback = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
            result = False

        return result

    def unique_identifier(self) -> str:
        """
        Used to identify a BMS when multiple BMS are connected
        If not provided by the BMS/driver then the hardware version and capacity is used,
        since it can be changed by small amounts to make a battery unique.
        On +/- 5 Ah you can identify 11 batteries
        """
        return self.address.replace(":", "").lower()

    async def send_command(self, command) -> Union[bytearray, bool]:
        if not self.bt_client:
            logger.error(">>> ERROR: No BLE client connection - returning")
            return False

        fut = self.bt_loop.create_future()

        def rx_callback(future: asyncio.Future, data: bytearray, sender, rx: bytearray):
            data.extend(rx)
            if len(data) < (self.LENGTH_POS + 1):
                return

            length = data[self.LENGTH_POS]
            if len(data) <= length + self.LENGTH_POS + 3:
                return
            if not future.done():
                future.set_result(data)

        rx_collector = functools.partial(rx_callback, fut, bytearray())
        await self.bt_client.start_notify(BLE_CHARACTERISTICS_RX_UUID, rx_collector)
        await self.bt_client.write_gatt_char(BLE_CHARACTERISTICS_TX_UUID, command, False)
        result = await fut
        await self.bt_client.stop_notify(BLE_CHARACTERISTICS_RX_UUID)

        # Heartbeat: bytes arrived from the BMS. The bt_main_loop's
        # while-loop polls _ble_data_is_stale() against this timestamp
        # to decide whether to force a reconnect.
        self.last_ble_data_received = time()

        return result

    async def async_read_serial_data_llt(self, command):
        if self.hci_uart_ok:
            try:
                bt_task = asyncio.run_coroutine_threadsafe(self.send_command(command), self.bt_loop)
                result = await asyncio.wait_for(asyncio.wrap_future(bt_task), 20)
                return result
            except asyncio.TimeoutError:
                get_connection_error_message(self.online)
                return False
            except BleakDBusError:
                exception_type, exception_object, exception_traceback = sys.exc_info()
                file = exception_traceback.tb_frame.f_code.co_filename
                line = exception_traceback.tb_lineno
                logger.error(f"BleakDBusError: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
                self.reset_bluetooth()
                return False
            except Exception:
                exception_type, exception_object, exception_traceback = sys.exc_info()
                file = exception_traceback.tb_frame.f_code.co_filename
                line = exception_traceback.tb_lineno
                logger.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
                self.reset_bluetooth()
                return False
        else:
            return False

    def read_serial_data_llt(self, command):
        if not self.bt_loop:
            return False
        try:
            data = asyncio.run(self.async_read_serial_data_llt(command))
            return self.validate_packet(data)
        except CancelledError as e:
            logger.error(">>> ERROR: No reply - canceled - returning")
            logger.error(e)
            return False
        # except Exception as e:
        #     get_connection_error_message(self.online)
        #     logger.error(e)
        #     return False
        except Exception:
            exception_type, exception_object, exception_traceback = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
            return False

    def reset_bluetooth(self):
        if not BLUETOOTH_FORCE_RESET_BLE_STACK:
            return

        self.bt_loop = False

        restart_ble_hardware_and_bluez_driver()

    async def reset_hci_uart(self) -> None:
        """Reset the HCI UART stack and BlueZ kernel modules in-process.

        Previously this method ended in ``self.run = False`` followed by
        ``sys.exit(1)``, forcing a supervisor restart of the whole driver.
        Now we tear down the link in-process, reload the kernel modules,
        restart hciattach, and let ``background_loop()``'s
        retry-with-backoff bring the BMS connection back online.

        Called from ``bt_main_loop()`` only when the BleakScanner
        exception text contains 'Bluetooth adapters' — i.e. when BlueZ
        itself reports no usable HCI device. Cheaper recovery paths
        (transient scan timeout, peripheral disconnect, heartbeat
        timeout) take the regular back-off route without touching kernel
        modules.

        ``async`` so the ~7s of total settle/wait time uses
        ``await asyncio.sleep`` instead of blocking ``time.sleep`` —
        otherwise the entire event loop (including the heartbeat poll
        for any sibling connection) would freeze during recovery.
        """
        logger.warning("Reset of hci_uart stack — will reconnect to: " + self.address)

        # Drop in-process state so any pending async reads bail out cleanly.
        self.bt_loop = None
        self.bt_client = None
        self.device = None

        # Tear down and reload the kernel-side stack. Args are static and
        # split into argv lists so we don't need a shell.
        for cmd in (
            ["pkill", "-f", "hciattach"],
            ["rmmod", "hci_uart"],
            ["rmmod", "btbcm"],
            ["modprobe", "hci_uart"],
            ["modprobe", "btbcm"],
        ):
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if cmd[0] == "pkill":
                await asyncio.sleep(0.5)
        await asyncio.sleep(2)  # let the modules settle before re-attaching the UART

        # Restart hciattach using the command captured at driver init
        # (see __init__ — /tmp/dbus-blebattery-hciattach holds the
        # original `ps -ww` row). Without this the modules are reloaded
        # but no userspace process is talking to the UART.
        if os.path.isfile("/tmp/dbus-blebattery-hciattach"):
            try:
                with open("/tmp/dbus-blebattery-hciattach", "r") as f:
                    hciattach_cmd = f.readline().strip()
                if hciattach_cmd:
                    # shlex.split + Popen with shell=False so we tokenize
                    # the saved command line instead of handing it to a
                    # shell (the file content originates from `ps -ww`,
                    # not user input, but no reason to add a shell layer).
                    # start_new_session detaches it so it survives this
                    # function returning.
                    subprocess.Popen(
                        shlex.split(hciattach_cmd),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    await asyncio.sleep(5)  # give hciattach a moment to bring the device up
            except Exception as e:
                logger.error(f"Failed to restart hciattach: {e}")
        else:
            logger.warning("No hciattach command saved at driver init; relying on system to re-spawn it")

        # Heartbeat reset so _ble_data_is_stale doesn't immediately fire
        # against the pre-reset timestamp on the first reconnect attempt.
        self.last_ble_data_received = time()

        # NOTE: self.run stays True. background_loop() will re-enter
        # bt_main_loop() and _reconnect_backoff() spaces out retries.


if __name__ == "__main__":
    bat = LltJbd_Ble("Foo", -1, sys.argv[1])
    if not bat.test_connection():
        logger.error(">>> ERROR: Unable to connect")
    else:
        # Allow to change charge / discharge FET
        bat.charge_fet = True
        bat.discharge_fet = True

        bat.trigger_disable_balancer = True
        bat.trigger_force_disable_charge = True
        bat.trigger_force_disable_discharge = True
        bat.refresh_data()
        bat.trigger_disable_balancer = False
        bat.trigger_force_disable_charge = False
        bat.trigger_force_disable_discharge = False
        bat.refresh_data()
        bat.get_settings()
