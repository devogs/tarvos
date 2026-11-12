import pytest
from syslog_configure import SyslogSettings, load_settings, render


def test_defaults_log_to_stdout_only():
    settings = load_settings({})
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
