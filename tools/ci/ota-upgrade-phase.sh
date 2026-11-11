#!/usr/bin/env bash
# Run one phase of the release -> target OTA upgrade test
# (PROVESFlightControllerReference/test/ota-upgrade/ota_upgrade_test.py).
#
# The release and the target speak different ground protocols (dictionary,
# deframer, framer plugin), so each phase starts a GDS from that image's own
# checkout: "release" runs from ota-from/ (the release tag, checked out by
# .github/workflows/ota-upgrade.yaml), "target" from this checkout. The GDS is
# stopped when the phase ends, pass or fail.
#
# Usage: ota-upgrade-phase.sh <release|target> <test function>
#        ota-upgrade-phase.sh stop
# Environment: OTA_IMAGE, OTA_BUILD_ID, OTA_STATE, OTA_DEST; optional
# OTA_PYTEST_ARGS (extra pytest arguments, split on whitespace).
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/../.." && pwd)
release_root="$repo_root/ota-from"
test_file="$repo_root/PROVESFlightControllerReference/test/ota-upgrade/ota_upgrade_test.py"
deployment="build-artifacts/zephyr/fprime-zephyr-deployment"
# Every phase begins after a reboot that swaps images, which keeps USB down
# for up to ~150 s (BOOT_TIMEOUT_SWAP_S in test/int/ota_update_test.py).
tty_wait_s=240

usage() {
    echo "usage: $0 <release|target> <test function> | $0 stop" >&2
    exit 2
}

stop_gds() {
    local root
    for root in "$repo_root" "$release_root"; do
        if [ -f "$root/server.pid" ]; then
            # The GDS may already have exited; the pkill below covers its children.
            kill "$(cat "$root/server.pid")" 2>/dev/null || true
            rm -f "$root/server.pid"
        fi
        # pkill exits 1 when nothing matched, which is the common case.
        pkill -9 -f "$root/fprime-venv/" || true
    done
    "$repo_root/tools/ci/kill-ci-processes.sh"
    if [ -n "${UART_DEVICE:-}" ]; then
        # Nothing may hold the port; fuser exits 1 then.
        fuser -k "$UART_DEVICE" 2>/dev/null || true
    fi
}

wait_for_tty() {
    local i
    for i in $(seq 1 "$tty_wait_s"); do
        # --once so the detector's own retries do not multiply this budget.
        if UART_DEVICE=$("$repo_root/tools/ci/detect-board-tty.sh" --once 2>/dev/null); then
            export UART_DEVICE
            echo "Board tty $UART_DEVICE after ${i}s"
            # As bootstrap-gds: let the app finish booting before the GDS opens the port.
            sleep 15
            return 0
        fi
        sleep 1
    done
    echo "ERROR: board tty did not enumerate within ${tty_wait_s}s" >&2
    return 1
}

if [ "${1:-}" = "stop" ]; then
    stop_gds
    exit 0
fi

[ $# -eq 2 ] || usage
ground=$1
phase=$2
[[ "$phase" =~ ^test_[a-z0-9_]+$ ]] || usage
: "${OTA_IMAGE:?}" "${OTA_BUILD_ID:?}" "${OTA_STATE:?}" "${OTA_DEST:?}"

stop_gds
wait_for_tty

case "$ground" in
    release)
        root=$release_root
        # The release's gds-integration target does not take UART_DEVICE.
        gds_args=(GDS_COMMAND="$root/fprime-venv/bin/fprime-gds --uart-device=$UART_DEVICE")
        ;;
    target)
        root=$repo_root
        gds_args=(UART_DEVICE="$UART_DEVICE")
        ;;
    *)
        usage
        ;;
esac

trap stop_gds EXIT
cd "$root"
# Pin the venv: a VIRTUAL_ENV from the runner's .env would otherwise win in
# both Makefiles (`?=`), and the two grounds must not share one.
export VIRTUAL_ENV="$root/fprime-venv"
nohup make gds-integration "${gds_args[@]}" >"$repo_root/gds-$ground-$phase.log" 2>&1 &
echo $! >"$root/server.pid"

read -r -a extra_args <<<"${OTA_PYTEST_ARGS:-}"
"$root/fprime-venv/bin/python" -m pytest "$test_file::$phase" \
    --deployment "$deployment" \
    --ota-image "$OTA_IMAGE" \
    --ota-build-id "$OTA_BUILD_ID" \
    --ota-dest "$OTA_DEST" \
    --ota-state "$OTA_STATE" \
    "${extra_args[@]}"
