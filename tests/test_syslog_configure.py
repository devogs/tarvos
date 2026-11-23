import pytest
from syslog_configure import SyslogSettings, load_settings, render


def test_defaults_log_to_stdout_only():
    settings = load_settings({}, isatty=lambda: False)
    conf = render(settings)
    assert settings.servers == ()
    assert "/dev/stdout" in conf
    assert "omfwd" not in conf


def test_multiple_servers_udp():
    settings = load_settings({"SYSLOG_SERVER": "[10.0.0.5, 10.0.0.6]", "SYSLOG_PORT": "1514"})
    conf = render(settings)
    assert conf.count('type="omfwd"') == 2
    assert 'target="10.0.0.5"' in conf
    assert 'port="1514"' in conf
    assert "queue.type" not in conf


def test_tcp_gets_reliable_queue():
    conf = render(SyslogSettings(("10.0.0.5",), 514, "tcp", "warning"))
    assert 'protocol="tcp"' in conf
    assert 'queue.type="LinkedList"' in conf
    assert "*.warning action(" in conf


@pytest.mark.parametrize(
    "env",
    [
        {"SYSLOG_SERVER": "10.0.0.1; evil"},
        {"SYSLOG_SERVER": "10.0.0.1", "SYSLOG_PORT": "99999"},
        {"SYSLOG_SERVER": "10.0.0.1", "SYSLOG_PROTOCOL": "carrier-pigeon"},
        {"SYSLOG_SERVER": "10.0.0.1", "SYSLOG_MIN_SEVERITY": 'info" action(type="omprog"'},
    ],
)
def test_invalid_input_disables_forwarding(env):
    settings = load_settings(env)
    assert settings.servers == ()
    assert "omfwd" not in render(settings)


def test_remote_collectors_get_rfc5424_with_pri():
    # Collectors need the PRI (facility and severity) and a standard format:
    # RFC 5424 to every remote server
    conf = render(SyslogSettings(("10.0.0.5", "10.0.0.6"), 514, "udp", "info"))
    assert conf.count('template="RSYSLOG_SyslogProtocol23Format"') == 2
    assert 'template="TarvosFmt"' in conf  # the local stdout stream stays human-readable
    conf = render(SyslogSettings(("10.0.0.5",), 514, "udp", "info", True))
    stdout = next(line for line in conf.splitlines() if "/dev/stdout" in line)
    assert 'template="TarvosFmt"' in stdout


def _tty(value):
    return lambda: value


def test_auto_with_terminal_skips_stdout_action():
    conf = render(load_settings({}, isatty=_tty(True)))
    assert "/dev/stdout" not in conf
    assert "TarvosFmt" in conf  # the template stays defined


def test_auto_without_terminal_writes_stdout_action():
    conf = render(load_settings({"SYSLOG_STDOUT": "auto"}, isatty=_tty(False)))
    assert 'file="/dev/stdout"' in conf


@pytest.mark.parametrize("isatty", [True, False])
def test_yes_forces_stdout_action(isatty):
    assert "/dev/stdout" in render(load_settings({"SYSLOG_STDOUT": "yes"}, isatty=_tty(isatty)))


@pytest.mark.parametrize("isatty", [True, False])
def test_no_never_writes_stdout_action(isatty):
    assert "/dev/stdout" not in render(load_settings({"SYSLOG_STDOUT": "no"}, isatty=_tty(isatty)))


def test_invalid_stdout_value_falls_back():
    env = {"SYSLOG_SERVER": "10.0.0.1", "SYSLOG_STDOUT": "maybe"}
    on_tty = load_settings(env, isatty=_tty(True))
    off_tty = load_settings(env, isatty=_tty(False))
    assert on_tty.servers == ()
    assert on_tty.stdout is False
    assert off_tty.stdout is True


def test_positional_settings_default_to_stdout():
    assert SyslogSettings((), 514, "udp", "info").stdout is True


def test_remote_forwarding_identical_in_every_mode():
    env = {"SYSLOG_SERVER": "[10.0.0.5, 10.0.0.6]", "SYSLOG_PROTOCOL": "tcp"}

    def fwd(extra, isatty):
        conf = render(load_settings({**env, **extra}, isatty=_tty(isatty)))
        return [line for line in conf.splitlines() if "omfwd" in line or "Remote" in line]

    reference = fwd({}, False)
    assert len(reference) == 4
    for extra in ({}, {"SYSLOG_STDOUT": "yes"}, {"SYSLOG_STDOUT": "no"}):
        for isatty in (True, False):
            assert fwd(extra, isatty) == reference
