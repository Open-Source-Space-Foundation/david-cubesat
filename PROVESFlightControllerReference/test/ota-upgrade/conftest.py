"""
conftest.py:

Options for ota_upgrade_test.py. Kept apart from test/int/conftest.py because
these phases also run under the release's fprime-gds, which that file may not
support.
"""

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class OtaUpgrade:
    """One upgrade run, shared by its four phases."""

    image: str  # signed target image to uplink
    build_id: str  # marker in the target's ProjectVersion
    dest: str  # where the image lands on the board's filesystem
    state: str  # JSON file that carries values from one phase to the next


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ota-image", help="Signed target image (zephyr.signed.bin).")
    parser.addoption(
        "--ota-build-id",
        help="Marker in CdhCore.version.ProjectVersion while the target runs.",
    )
    parser.addoption(
        "--ota-dest",
        # 8.3 name: the release's FatFs may be built without long file names.
        default="/otaupg.bin",
        help="Board path the image is uplinked to.",
    )
    parser.addoption(
        "--ota-state",
        help="File that carries the release's version from phase 1 to phase 3.",
    )


@pytest.fixture
def ota_upgrade(request: pytest.FixtureRequest) -> OtaUpgrade:
    """Return the run's options; fail when one is missing (the phases are CI-only)."""
    names = ("--ota-image", "--ota-build-id", "--ota-dest", "--ota-state")
    values = {name: request.config.getoption(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.fail(f"ota_upgrade_test.py needs {', '.join(missing)}")
    return OtaUpgrade(
        image=values["--ota-image"],
        build_id=values["--ota-build-id"],
        dest=values["--ota-dest"],
        state=values["--ota-state"],
    )
