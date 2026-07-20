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
    def __init__(self, exc: Exception | None, response: str = "ok output",
                 responses: list[str] | None = None, prompt: str = "sw:1#",
                 enable_exc: Exception | None = None):
        self.exc = exc
        self.response = response
        self.responses = responses      # per-call responses, if given
        self.prompt = prompt
        self.enable_exc = enable_exc
        self.calls = 0
        self.enable_calls = 0
        self.channel_writes: list[str] = []

    def send_command(self, command, read_timeout=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        if self.responses is not None:
            return self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return self.response

    def find_prompt(self):
        return self.prompt

    def enable(self, cmd="enable"):
        self.enable_calls += 1
        if self.enable_exc:
            raise self.enable_exc
        self.prompt = self.prompt.rstrip(">#") + "#"

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


def test_paging_disable_falls_back_to_abbreviated_form():
    # full 'terminal more disable' rejected, field-verified 'term more dis' ok
    conn = _FakeConn(None, responses=[
        "% Invalid input detected at '^' marker.", ""])
    runner = _make_runner(conn)
    runner._ensure_paging_disabled()
    assert runner.setup_warnings == []
    assert conn.calls == 2


def test_already_privileged_prompt_skips_enable():
    conn = _FakeConn(None, prompt="gx-11-s72-p1:1#")
    runner = _make_runner(conn)
    runner._ensure_privileged()
    assert conn.enable_calls == 0
    assert runner.setup_warnings == []


def test_unprivileged_prompt_triggers_enable():
    # SSH login lands in user EXEC ('>') - show interfaces/lldp do not exist
    # there; the runner must send 'enable' exactly like an operator's 'ena'
    conn = _FakeConn(None, prompt="gx-11-s72-p1:1>")
    runner = _make_runner(conn)
    runner._ensure_privileged()
    assert conn.enable_calls == 1
    assert conn.prompt.endswith("#")
    assert runner.setup_warnings == []


def test_enable_failure_is_a_visible_warning():
    conn = _FakeConn(None, prompt="gx-11-s72-p1:1>",
                     enable_exc=ValueError("Failed to enter enable mode."))
    runner = _make_runner(conn)
    runner._ensure_privileged()
    assert len(runner.setup_warnings) == 1
    assert "privileged EXEC" in runner.setup_warnings[0]
    assert "access level" in runner.setup_warnings[0]


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


import io


def test_patient_ers_class_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda *_: None)
    cls = connection._patient_ers_class()
    assert cls is not None
    inst = object.__new__(cls)
    calls = {"n": 0}

    def flaky_set_base_prompt():
        calls["n"] += 1
        if calls["n"] < 3:            # miss the prompt on the first two tries
            raise TimeoutError("Pattern not detected: (?:\\#|>)")

    inst.set_base_prompt = flaky_set_base_prompt
    inst.set_terminal_width = lambda: None
    inst.disable_paging = lambda: None
    inst.clear_buffer = lambda: ""
    inst.write_channel = lambda _d: None
    inst.RETURN = "\n"
    inst.session_preparation()        # must not raise
    assert calls["n"] == 3


def test_patient_ers_class_gives_up_and_reraises(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda *_: None)
    cls = connection._patient_ers_class()
    inst = object.__new__(cls)

    def always_fail():
        raise TimeoutError("Pattern not detected: (?:\\#|>)")

    inst.set_base_prompt = always_fail
    inst.clear_buffer = lambda: ""
    inst.write_channel = lambda _d: None
    inst.RETURN = "\n"
    with pytest.raises(TimeoutError):
        inst.session_preparation()


def test_fail_keeps_banner_out_of_report_but_logs_it(caplog):
    # the noisy login banner must NOT appear in the error shown in the report;
    # it goes to the log instead, with a short prompt/Ctrl-Y hint on the error
    runner = _make_runner(_FakeConn(None))
    runner._session_log = io.BytesIO(
        b"\n=== MOTD ===\nWelcome\nEnter Ctrl-Y to begin\n")
    with caplog.at_level("WARNING"):
        err = runner._fail(TimeoutError("Pattern not detected: (?:\\#|>)\n\n"
                                        "Things you might try...\n1. ...\n2. ..."))
    msg = str(err)
    assert "MOTD" not in msg and "Welcome" not in msg   # banner not in report
    assert "Things you might try" not in msg            # netmiko blob trimmed
    assert "Ctrl-Y" in msg                              # concise actionable hint
    assert "MOTD" in caplog.text                        # banner IS in the log


def test_fail_no_hint_and_no_banner_when_device_silent():
    # a true kex/cipher rejection produces no channel bytes -> no prompt hint
    runner = _make_runner(_FakeConn(None))
    runner._session_log = io.BytesIO(b"")
    err = runner._fail(Exception("Incompatible ssh peer (no acceptable kex)"))
    assert "Ctrl-Y" not in str(err)
    assert "Incompatible ssh peer" in str(err)


def test_clean_reason_takes_only_the_first_line():
    reason = SshRunner._clean_reason(
        ValueError("boom\nsecond line\nthird line"))
    assert reason == "ValueError: boom"


def test_patient_ers_special_login_presses_ctrl_y(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda *_: None)
    cls = connection._patient_ers_class()
    inst = object.__new__(cls)
    inst.prompt_pattern = r"(?m:[>#]\s*$)"
    inst.RETURN = "\n"
    inst.username, inst.password = "admin", "secret"
    writes: list[str] = []
    inst.write_channel = lambda d: writes.append(d)
    # device: silent, then the Ctrl-Y banner, then finally a prompt
    reads = iter([TimeoutError("silent"),
                  "\nEnter Ctrl-Y to begin\n",
                  "switch-01#"])

    def fake_read(pattern="", read_timeout=0.0):
        nxt = next(reads)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    inst.read_until_pattern = fake_read
    inst.special_login_handler()                 # must return, not hang/raise
    assert connection._CTRL_Y in writes          # Ctrl-Y was actually sent
