"""
ota_upgrade_test.py:

Upgrade a board running the latest release to the target build (main or a PR)
over the air, as an operator would upgrade a satellite in orbit.

The release and the target use different ground stacks (dictionary, deframer,
framer plugin), so each phase is its own pytest run against a GDS started from
that image's checkout. The phases run in order:

1. release: uplink the target image, stage it, arm a TEST boot, reboot.
2. target:  the TEST image booted; reboot without confirming.
3. release: MCUBoot reverted to the release; re-arm the TEST boot, reboot.
4. target:  confirm, reboot, the target persists.

tools/ci/ota-upgrade-phase.sh runs one phase; .github/workflows/ota-upgrade.yaml
runs all four. This file runs under both stacks' fprime-gds, so it uses only
fprime-gds API that both provide and imports nothing from test/int.
"""

import json
import os
import shutil
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import datetime

from fprime_gds.common.files.helpers import FileStates
from fprime_gds.common.models.serialize.time_type import TimeType
from fprime_gds.common.testing_fw.api import IntegrationTestAPI

UPDATER = "Update.updater"
VERSION = "CdhCore.version"
RESET_MANAGER = "ReferenceDeployment.resetManager"
FILE_MANAGER = "FileHandling.fileManager"
UART_ONLY_DOWNLINK = "ReferenceDeployment.downlinkRepeater.CHANNEL_ENABLED_PRM_SET"

COMMAND_TIMEOUT_S = 10
PREPARE_TIMEOUT_S = 60
UPDATE_TIMEOUT_S = 180
# First contact after a flash, and after a reboot that swaps images.
LINK_TIMEOUT_S = 120
SWAP_LINK_TIMEOUT_S = 300
BOOT_TIMEOUT_PLAIN_S = 60
# ~35 min for a ~676KB image at --file-uplink-cooldown 0.6.
UPLINK_TIMEOUT_S = 3600

# The framer plugin of either stack reads its next sequence number from here,
# relative to the checkout the GDS runs from.
SEQUENCE_NUMBER_FILE = "./Framing/src/sequence_number.bin"


@dataclass(frozen=True)
class SeqSource:
    """Where a ground stack learns the board's authentication sequence number."""

    command: str  # a GET_SEQ_NUM the board accepts unauthenticated
    event: str  # the event that reports the number
    ground_offset: int  # added to the board's number to get ground's next


SEQ_SOURCES = (
    # Target: the UART deframer reports the last accepted number (test/int/common.py).
    SeqSource(
        "ComCcsdsUart.tcSecurityDeframer.GET_SEQ_NUM",
        "ComCcsdsUart.tcSecurityDeframer.SequenceNumberGet",
        1,
    ),
    # v1.0.x releases: only the LoRa instance's GET_SEQ_NUM bypasses
    # authentication, and the release's own sync writes the number unchanged.
    SeqSource(
        "ComCcsdsLora.authenticatelora.GET_SEQ_NUM",
        "ComCcsdsLora.authenticatelora.EmitSequenceNumber",
        0,
    ),
)


def _has_command(api: IntegrationTestAPI, command: str) -> bool:
    return command in api.pipeline.dictionaries.command_name


def _seq_source(api: IntegrationTestAPI) -> SeqSource:
    found = [s for s in SEQ_SOURCES if _has_command(api, s.command)]
    assert found, f"dictionary has none of {[s.command for s in SEQ_SOURCES]}"
    return found[0]


def _at_or_after(a: int, b: int) -> bool:
    """U32 serial-number order (RFC 1982), as the board's validator uses."""
    return (a - b) % 2**32 < 2**31


def _read_ground_next() -> int | None:
    try:
        with open(SEQUENCE_NUMBER_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        # No usable ground number: the board's wins.
        return None


def _write_ground_next(n: int) -> None:
    """Rename over the file so the framer never reads a truncated one."""
    temporary = f"{SEQUENCE_NUMBER_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(str(n))
    os.replace(temporary, SEQUENCE_NUMBER_FILE)


def _board_seq(api: IntegrationTestAPI, source: SeqSource) -> int | None:
    api.clear_histories()
    api.send_command(source.command)
    evt = api.await_event(source.event, timeout=5)
    return None if evt is None else int(evt.args[0].val)


def _resync(api: IntegrationTestAPI) -> None:
    """Fast-forward ground's sequence number if the board is ahead."""
    source = _seq_source(api)
    board = _board_seq(api, source)
    assert board is not None, f"no {source.event} within 5 s"
    want = (board + source.ground_offset) % 2**32
    ground = _read_ground_next()
    if ground is None or _at_or_after(want, ground):
        _write_ground_next(want)


def _wait_for_link(api: IntegrationTestAPI, timeout_s: float) -> None:
    """Wait for the board to answer (it may still be swapping images), then resync."""
    source = _seq_source(api)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _board_seq(api, source) is not None:
            _resync(api)
            return
        time.sleep(2)
    raise AssertionError(f"board did not answer {source.command} in {timeout_s} s")


def _project_version(api: IntegrationTestAPI) -> str:
    for _ in range(3):
        api.clear_histories()
        api.send_command(f"{VERSION}.VERSION", ["PROJECT"])
        evt = api.await_event(f"{VERSION}.ProjectVersion", timeout=10)
        if evt is not None:
            return str(evt.args[0].val)
        _resync(api)
    raise AssertionError("no ProjectVersion event from the board")


def _apply_uart_only_downlink(api: IntegrationTestAPI) -> None:
    """Downlink over UART only where the image has the (RAM-only) parameter."""
    if not _has_command(api, UART_ONLY_DOWNLINK):
        return
    # [uart, lora, sband]; array arguments are passed as a JSON string.
    api.send_command(UART_ONLY_DOWNLINK, ['["ENABLED", "DISABLED", "DISABLED"]'])
    time.sleep(1)


def _command_and_await(
    api: IntegrationTestAPI, command: str, args: list[str], event: str, timeout_s: float
) -> None:
    api.clear_histories()
    api.send_and_assert_command(command, args, timeout=COMMAND_TIMEOUT_S)
    api.assert_event(event, timeout=timeout_s)


def _arm_test_boot(api: IntegrationTestAPI) -> None:
    _command_and_await(
        api,
        f"{UPDATER}.CONFIGURE_NEXT_BOOT",
        ["TEST"],
        f"{UPDATER}.SetNextBoot",
        COMMAND_TIMEOUT_S,
    )


def _cold_reset(api: IntegrationTestAPI) -> TimeType:
    """Resync, send COLD_RESET (it never completes), and return the send time."""
    _resync(api)
    start = TimeType().set_datetime(
        datetime.now(), time_base=TimeType.TimeBase("TB_DONT_CARE")
    )
    api.send_command(f"{RESET_MANAGER}.COLD_RESET")
    return start


def _local_crc(path: str) -> int:
    """CRC32 as the board's FlashWorker computes it (zlib.crc32 ^ 0xFFFFFFFF)."""
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(8192):
            crc = zlib.crc32(chunk, crc)
    return ~crc & 0xFFFFFFFF


def _uplink(api: IntegrationTestAPI, local: str, dest: str) -> bool:
    """Uplink a copy of ``local`` (the uplinker deletes its source); True once IDLE."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        staged = tmp.name
    shutil.copyfile(local, staged)
    uplinker = api.pipeline.files.uplinker
    uplinker.enqueue(staged, dest)
    deadline = time.monotonic() + UPLINK_TIMEOUT_S
    time.sleep(1)
    while time.monotonic() < deadline and uplinker.state != FileStates.IDLE:
        time.sleep(1)
    return uplinker.state == FileStates.IDLE


def _write_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _read_state(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_release_stages_target(fprime_test_api: IntegrationTestAPI, ota_upgrade):
    """Phase 1 (release ground): stage the target in slot1 and boot it once."""
    api = fprime_test_api
    _wait_for_link(api, LINK_TIMEOUT_S)
    release = _project_version(api)
    assert ota_upgrade.build_id not in release, (
        f"board already runs {release!r}; flash the release first"
    )
    _write_state(ota_upgrade.state, {"release_version": release})

    # The release's FileManager has no CalculateCrc; the FlashWorker checks
    # this CRC against the uplinked file before writing slot1.
    crc = _local_crc(ota_upgrade.image)
    assert _uplink(api, ota_upgrade.image, ota_upgrade.dest), (
        f"uplinker did not return to IDLE for {ota_upgrade.dest}"
    )
    _command_and_await(
        api,
        f"{UPDATER}.PREPARE_UPDATE",
        [],
        f"{UPDATER}.PrepareUpdateSucceeded",
        PREPARE_TIMEOUT_S,
    )
    _command_and_await(
        api,
        f"{UPDATER}.UPDATE_IMAGE_FROM",
        [ota_upgrade.dest, str(crc)],
        f"{UPDATER}.UpdateSucceeded",
        UPDATE_TIMEOUT_S,
    )
    _arm_test_boot(api)
    _cold_reset(api)


def test_target_boots_unconfirmed(fprime_test_api: IntegrationTestAPI, ota_upgrade):
    """Phase 2 (target ground): the TEST image runs; reboot without confirming."""
    api = fprime_test_api
    _wait_for_link(api, SWAP_LINK_TIMEOUT_S)
    _apply_uart_only_downlink(api)
    version = _project_version(api)
    assert ota_upgrade.build_id in version, (
        f"board runs {version!r}, not the target {ota_upgrade.build_id!r}: "
        "the release did not boot the staged image"
    )
    _cold_reset(api)


def test_release_after_revert(fprime_test_api: IntegrationTestAPI, ota_upgrade):
    """Phase 3 (release ground): MCUBoot reverted; re-arm the target in slot1."""
    api = fprime_test_api
    _wait_for_link(api, SWAP_LINK_TIMEOUT_S)
    release = _read_state(ota_upgrade.state)["release_version"]
    version = _project_version(api)
    assert version == release, (
        f"board runs {version!r}, not the release {release!r}: "
        "MCUBoot did not revert the unconfirmed target"
    )
    # The revert swapped the target back into slot1.
    _arm_test_boot(api)
    _cold_reset(api)


def test_target_confirms(fprime_test_api: IntegrationTestAPI, ota_upgrade):
    """Phase 4 (target ground): confirm the target; it survives a reboot."""
    api = fprime_test_api
    _wait_for_link(api, SWAP_LINK_TIMEOUT_S)
    _apply_uart_only_downlink(api)
    version = _project_version(api)
    assert ota_upgrade.build_id in version, (
        f"board runs {version!r}, not the target {ota_upgrade.build_id!r} "
        "after re-arming it"
    )
    _command_and_await(
        api,
        f"{UPDATER}.CONFIRM_UPDATE",
        [],
        f"{UPDATER}.ConfirmBoot",
        COMMAND_TIMEOUT_S,
    )

    start = _cold_reset(api)
    booted = api.await_event(
        f"{VERSION}.FrameworkVersion", start=start, timeout=BOOT_TIMEOUT_PLAIN_S
    )
    assert booted is not None, f"no boot within {BOOT_TIMEOUT_PLAIN_S} s of COLD_RESET"
    _resync(api)
    _apply_uart_only_downlink(api)
    confirmed = _project_version(api)
    assert ota_upgrade.build_id in confirmed, (
        f"board runs {confirmed!r} after CONFIRM_UPDATE and a reboot: "
        "the confirmed target did not persist"
    )

    _command_and_await(
        api,
        f"{FILE_MANAGER}.RemoveFile",
        [ota_upgrade.dest, "false"],
        f"{FILE_MANAGER}.RemoveFileSucceeded",
        COMMAND_TIMEOUT_S,
    )
