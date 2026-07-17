import paramiko
import pytest
from paramiko.transport import Transport

from switch_migrator import connection
from switch_migrator.connection import (
    _LEGACY_CIPHERS,
    _LEGACY_KEX,
    _LEGACY_KEYS,
    enable_legacy_ssh_algorithms,
)


@pytest.fixture(autouse=True)
def reset_legacy_flag(monkeypatch):
    monkeypatch.setattr(connection, "_legacy_enabled", False)


def test_enable_legacy_ssh_algorithms():
    enable_legacy_ssh_algorithms()
    # everything this paramiko build implements must now be negotiable
    for algo in _LEGACY_KEX:
        if algo in Transport._kex_info:
            assert algo in Transport._preferred_kex
    for algo in _LEGACY_CIPHERS:
        if algo in Transport._cipher_info:
            assert algo in Transport._preferred_ciphers
    for algo in _LEGACY_KEYS:
        if algo in Transport._key_info:
            assert algo in Transport._preferred_keys


def test_legacy_algorithms_appended_not_prepended():
    enable_legacy_ssh_algorithms()
    # modern algorithms must keep priority: the first preferences stay strong
    assert Transport._preferred_kex[0] not in _LEGACY_KEX
    assert Transport._preferred_ciphers[0] not in _LEGACY_CIPHERS
    assert Transport._preferred_keys[0] not in _LEGACY_KEYS


def test_enable_legacy_is_idempotent():
    enable_legacy_ssh_algorithms()
    kex_after_first = Transport._preferred_kex
    assert enable_legacy_ssh_algorithms() == []
    assert Transport._preferred_kex == kex_after_first
    # and no duplicates ever
    assert len(Transport._preferred_kex) == len(set(Transport._preferred_kex))


def test_paramiko_supports_ers_host_keys():
    # the requirements pin paramiko<4 precisely because 4.x dropped ssh-dss;
    # this guards against an accidental future upgrade breaking old ERS gear
    assert int(paramiko.__version__.split(".")[0]) < 4
    assert "ssh-dss" in Transport._key_info
