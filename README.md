# dbus-serialbattery

This driver is for Venus OS devices (any GX device sold by Victron or a Raspberry Pi running the Venus OS image).

The driver will communicate with a Battery Management System (BMS) that support serial (RS232, RS485 or TTL UART) and Bluetooth communication (see [BMS feature comparison](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/features#bms-feature-comparison) for details). The data is then published to the Venus OS system (dbus). The main purpose is to act as a Battery Monitor in your GX and supply State of Charge (SoC) and other values to the inverter/charger.

---

## How this fork differs from upstream

This is a focused fork of [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery) that hardens the **LLT/JBD BLE driver** for use on a boat where supervisor-restart cycles aren't acceptable as a recovery mechanism. The full list lives in [`CHANGELOG.md`](CHANGELOG.md); the highlights:

### Reliability fixes (LLT/JBD BLE)

The upstream driver kills itself with `sys.exit(1)` or `self.run = False` on every transient BLE error and relies on the systemd/daemontools supervisor to bring it back up. On a boat where the BMS connection is the safety-relevant link, that's a costly recovery path. This fork replaces the crash-and-restart pattern with in-process recovery:

- **Transient scanner / connect errors** (asyncio TimeoutError, generic exceptions, "no device found") no longer set `self.run = False`. Instead `bt_main_loop()` returns and `background_loop()` retries with an exponential back-off (5s → doubles → capped at 60s). The back-off resets to 5s on the first successful connection so a single hiccup doesn't permanently slow recovery.
- **`on_disconnect` callback** now invalidates `bt_loop` so concurrent `read_serial_data_llt()` calls short-circuit on the existing `if not self.bt_loop` guard instead of dispatching coroutines onto a dead event loop.
- **BLE-level heartbeat** (`BLE_HEARTBEAT_TIMEOUT_S = 15s`) detects "silently dead" connections — `BleakClient.is_connected` keeps reporting True (BlueZ hasn't torn down the link yet) but no bytes flow from the BMS. `bt_main_loop()`'s while-loop polls `_ble_data_is_stale()` and breaks out to trigger reconnect when the timeout passes.
- **`reset_hci_uart`** no longer calls `sys.exit(1)`. Kernel-module reload (`rmmod hci_uart`/`btbcm`, `modprobe ...`) and hciattach restart now happen in-process via the `subprocess` module (with `shlex.split` and `start_new_session=True` for the hciattach respawn). The driver stays alive and `_reconnect_backoff()` paces the retries.
- Migrated the previous shell-out invocations to `subprocess.run` with argv lists for shell-injection safety.

### API additions (additive — no breaking changes)

- **`/LastUpdate` dbus path** holding the Unix timestamp (int seconds) of the last successful BMS refresh. Lets the Cerbo and any external consumer detect data staleness independently of the existing `error["count"]` and `BLOCK_ON_DISCONNECT` machinery. Backed by a new `Battery.last_successful_refresh: Union[float, None]` attribute on the base class, set by `DbusHelper.publish_battery()` after each successful `refresh_data()`.

### Quality improvements

- **21 new unit tests** under `tests/bms/test_lltjbd_ble.py` and `tests/test_last_successful_refresh.py`, covering the back-off mechanics, heartbeat predicate, on_disconnect cleanup, in-process HCI reset, and the `/LastUpdate` contract. Bleak is stubbed via `sys.modules` so the suite runs on any developer machine without BlueZ or hardware. **123 tests green** total (was 102 on upstream).

### What did NOT change

- All other BMS protocols (JKBMS, Daly, JBD non-BLE, etc.) are untouched — no functional changes outside `bms/lltjbd_ble.py`, `battery.py`, and `dbushelper.py`.
- The Venus OS install procedure, dbus path layout, configuration file format, and `BLOCK_ON_DISCONNECT_*` mechanics are all unchanged. Existing `config.ini` files keep working.
- The driver still imports cleanly without `bleak` installed locally — same `sys.modules` stub trick as upstream's `tests/conftest.py`.

### Status

**Pre-hardware-test.** The watchdog stack is software-validated by the test suite but not yet exercised on a real Cerbo + JBD BMS over a multi-day soak. PRs back to upstream are deferred until that hardware validation is done.

---

## History

The first version of this driver was released by [Louisvdw](https://github.com/Louisvdw/dbus-serialbattery) in September 2020.

In February 2023 I ([mr-manuel](https://github.com/mr-manuel)) made my first PR, since Louis did not have time anymore to contribute to this project.

With the release of `v1.0.0` I became the main developer of this project. From then on, I have been maintaining the project and developing it further. I'm also solving 99% of the issues on GitHub.

A big thanks to [Louisvdw](https://github.com/Louisvdw/dbus-serialbattery) for the initiation of this project.

## Support this project

This project takes a lot of time and effort to maintain, answering support requests, adding new features and so on.
If you are using this driver and you are happy with it, please make a donation to support me and this project.

[<img src="https://github.md0.eu/uploads/donate-button.svg" height="38">](https://www.paypal.com/donate/?hosted_button_id=3NEVZBDM5KABW)

## Documentation

* [Introduction](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/)
* [Features](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/features)
* [Supported BMS](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/supported-bms)
* [How to connect and prepare the battery/BMS](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/connect)
* [How to install, update, disable, enable and uninstall](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/install)
* [How to troubleshoot](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/troubleshoot/)
* [FAQ](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/faq/)

### Developer Remarks

To develop this project, install the requirements. This project makes use of velib_python which is pre-installed on
Venus-OS Devices under `/opt/victronenergy/dbus-systemcalc-py/ext/velib_python`. To use the python files locally,
`git clone` the [velib_python](https://github.com/victronenergy/velib_python) project to velib_python and add
velib_python to the `PYTHONPATH` environment variable.

Make sure the GitHub Actions run fine in your repository. In order to make the GitHub Actions run please select in your repository settings under `Actions` -> `General` -> `Actions permissions` the option `Allow all actions and reusable workflows`. Check also in your repository settings under `Actions` -> `General` -> `Workflow permissions` if `Read and write permissions` are selected. This will check your code for Flake8 and Black Lint errors. [Here](https://py-vscode.readthedocs.io/en/latest/files/linting.html) is a short instruction on how to set up Flake8 and Black Lint checks in VS Code. This will save you a lot of time.

See this checklist, if you want to [add a new BMS](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/supported-bms#add-by-opening-a-pull-request)

#### How it works

* Each supported BMS needs to implement the abstract base class `Battery` from `battery.py`.
* `dbus-serialbattery.py` tries to figure out the correct connected BMS by looping through all known implementations of `Battery` and executing its `test_connection()`. If this returns true, `dbus-serialbattery.py` sticks with this battery and then periodically executes `dbushelpert.publish_battery()`. `publish_battery()` executes `Battery.refresh_data()` which updates the fields of Battery. It then publishes those fields to dbus using `dbushelper.publish_dbus()`
* The Victron Device will be "controlled" by the values published on `/Info/` - namely:
  * `/Info/MaxChargeCurrent `
  * `/Info/MaxDischargeCurrent`
  * `/Info/MaxChargeVoltage`
  * `/Info/BatteryLowVoltage`
  * `/Info/ChargeRequest` (not implemented in dbus-serialbattery)

For more details on the Victron dbus interface see [the official victron dbus documentation](https://github.com/victronenergy/venus/wiki/dbus)

## Try it out — no setup required!

Curious what dbus-serialbattery looks and feels like in action? Explore the interactive demo and play around with all the settings — completely risk-free. Nothing is saved, nothing can go wrong, and no device is needed. It's a great way to get familiar with the interface before or during your own setup.

[Launch the interactive demo](https://mr-manuel.github.io/venus-os_gui-v2/)

## Join the community on Discord

https://discord.gg/YXzFB8rSgx

## Help translating to your language

Are you using this driver and you would like to have it in your language? Now you can help to translate it on [POEditor](https://poeditor.com/join/project/sA2DhyEpYh).
