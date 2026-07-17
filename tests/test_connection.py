import paramiko
import pytest
from paramiko.transport import Transport

from switch_migrator import connection
from switch_migrator.config import SshSettings
from switch_migrator.connection import (
    _LEGACY_CIPHERS,
    _LEGACY_KEX,
    _LEGACY_KEYS,
    CommandError,
    SshRunner,
    enable_legacy_ssh_algorithms,
    looks_like_error,
)

BANNER = (
    "*" * 84 + "\n"
    "                Command Execution Time: Thu Jul 16 13:42:31 2026 CEST\n"
    + "*" * 84 + "\n"
)


def test_looks_like_error_plain():
    assert looks_like_error("                     ^\n"
                            "% Invalid input detected at '^' marker.")


def test_looks_like_error_behind_banner():
    # the banner is longer than the old 200-char head scan - the error line
    # after it must still be detected
    assert looks_like_error(BANNER + "% Invalid input detected at '^' marker.")


def test_looks_like_error_negative():
    assert not looks_like_error(BANNER + "PORT NUM   ADMINSTATUS\n1/1   up   up")
    # '%' inside normal table data must not trip the detector
    assert not looks_like_error("utilization: 42 %\nsome other line")


class _FakeConn:
    def __init__(self, exc: Exception | None, response: str = "ok output"):
        self.exc = exc
        self.response = response
        self.calls = 0
        self.channel_writes: list[str] = []

    def send_command(self, command, read_timeout=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.response

    def write_channel(self, data):
        self.channel_writes.append(data)

    def clear_buffer(self):
        pass


def _make_runner(conn: _FakeConn) -> SshRunner:
    runner = object.__new__(SshRunner)
    runner.name = "test"
    runner.host = "test"
    runner.platform = connection.Platform.VOSS
    runner.ssh = SshSettings()
    runner.raw_dir = None
    runner.setup_warnings = []
    runner._transport_failures = 0
    runner._dead = False
    runner._conn = conn
    return runner


def test_paging_disable_verified_ok():
    conn = _FakeConn(None, response="")
    runner = _make_runner(conn)
    runner._ensure_paging_disabled()
    assert runner.setup_warnings == []
    assert conn.calls == 1


def test_paging_disable_rejection_is_surfaced():
    conn = _FakeConn(None, response="% Invalid input detected at '^' marker.")
    runner = _make_runner(conn)
    runner._ensure_paging_disabled()
    assert len(runner.setup_warnings) == 1
    assert "terminal more disable" in runner.setup_warnings[0]
    assert "may stall" in runner.setup_warnings[0]


def test_stuck_pager_is_quit_after_transport_failure():
    conn = _FakeConn(TimeoutError("Pattern not detected"))
    runner = _make_runner(conn)
    with pytest.raises(CommandError):
        runner.run("show interfaces gigabitEthernet state")
    # recovery: 'q' sent to kill a possible --More-- pager
    assert conn.channel_writes == ["q\n"]


def test_circuit_breaker_abandons_dead_session():
    conn = _FakeConn(OSError("socket closed"))
    runner = _make_runner(conn)
    for _ in range(2):
        with pytest.raises(CommandError):
            runner.run("show mlt")
    assert runner._dead
    # further commands are skipped instantly without touching the transport
    with pytest.raises(CommandError) as excinfo:
        runner.run("show vlan")
    assert "abandoned" in excinfo.value.output
    assert conn.calls == 2


def test_circuit_breaker_resets_on_success():
    conn = _FakeConn(OSError("hiccup"))
    runner = _make_runner(conn)
    with pytest.raises(CommandError):
        runner.run("show mlt")
    conn.exc = None
    assert runner.run("show vlan") == "ok output"
    assert runner._transport_failures == 0
    assert not runner._dead


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
