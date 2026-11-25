"""One adapter per platform: the commands it needs and how it reads them (decision 0006)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from migration_check.model import DeviceState, Platform
from migration_check.platforms import boss, fabric_engine
from migration_check.snapshot import RawDevice


@dataclass(frozen=True, slots=True)
class Adapter:
    commands: tuple[str, ...]  # sent by `snap`, in this order
    read_device: Callable[[RawDevice], DeviceState]
    netmiko_device_type: str


ADAPTERS = {
    Platform.FABRIC_ENGINE: Adapter(
        fabric_engine.COMMANDS, fabric_engine.read_device, fabric_engine.NETMIKO_DEVICE_TYPE
    ),
    Platform.BOSS: Adapter(boss.COMMANDS, boss.read_device, boss.NETMIKO_DEVICE_TYPE),
}


def read_device(raw: RawDevice) -> DeviceState:
    return ADAPTERS[raw.platform].read_device(raw)
