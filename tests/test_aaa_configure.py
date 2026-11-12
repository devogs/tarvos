import stat

import pytest
from aaa_configure import (
    AaaSettings,
    ConfigError,
    configure,
    load_settings,
    parse_tacacs_servers,
    render_nsswitch,
    render_pam,
    validate_secret,
)


@pytest.mark.parametrize(
    "raw",
    [
        '["10.0.0.10", "10.0.0.11"]',
        "[10.0.0.10, 10.0.0.11]",
        "10.0.0.10,10.0.0.11",
        "10.0.0.10 10.0.0.11",
        "['10.0.0.10', '10.0.0.11']",
    ],
)
def test_parse_formats(raw):
    assert parse_tacacs_servers(raw) == ["10.0.0.10", "10.0.0.11"]


@pytest.mark.parametrize("raw", [None, "", "  ", "[]"])
def test_parse_empty(raw):
    assert parse_tacacs_servers(raw) == []


def test_parse_dedupes_and_supports_ipv6():
    assert parse_tacacs_servers("10.0.0.1, 10.0.0.1, 2001:db8::1") == ["10.0.0.1", "2001:db8::1"]


@pytest.mark.parametrize(
    "raw",
    [
        "<primary_ip>",
        "10.0.0.1; rm -rf /",
        "10.0.0.256",
        "host.example.com",
        "10.0.0.1 server=evil",
    ],
)
def test_parse_rejects_invalid(raw):
    with pytest.raises(ConfigError):
        parse_tacacs_servers(raw)


@pytest.mark.parametrize("secret", [None, "", "has space", "new\nline", "x" * 129])
def test_secret_rejected(secret):
    with pytest.raises(ConfigError):
        validate_secret(secret)


def test_invalid_config_falls_back_to_local():
    settings = load_settings({"TACACS_SERVER": "<primary_ip>", "TACACS_SECRET": "s3cret"})
    assert not settings.tacacs_enabled


def test_missing_secret_falls_back_to_local():
    assert not load_settings({"TACACS_SERVER": "10.0.0.1"}).tacacs_enabled


def test_pam_local_only_has_no_tacplus():
    pam = render_pam(AaaSettings((), "", 3), with_session=True)
    assert "pam_tacplus" not in pam
    assert "pam_unix.so" in pam


def test_pam_tacacs_failover_order_and_admin_bypass():
    pam = render_pam(AaaSettings(("10.0.0.1", "10.0.0.2"), "s3cret", 5), with_session=False)
    assert "server=10.0.0.1 server=10.0.0.2" in pam
    assert "timeout=5" in pam
    assert "user = admin" in pam  # break-glass account skips TACACS+
    assert "session" not in pam


def test_nsswitch_toggle_is_idempotent():
    base = "passwd:         files\ngroup:          files\n"
    enabled = render_nsswitch(base, tacacs_enabled=True)
    assert "passwd:         files ato" in enabled
    assert render_nsswitch(enabled, tacacs_enabled=True) == enabled
    assert render_nsswitch(enabled, tacacs_enabled=False) == base


def test_configure_writes_secret_files_0600(tmp_path):
    env = {"TACACS_SERVER": "[10.0.0.1, 10.0.0.2]", "TACACS_SECRET": "s3cret", "SSH_PORT": "2200"}
    configure(env, tmp_path, set_password=False)
    for name in ("sshd", "netopeer2"):
        path = tmp_path / "etc/pam.d" / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "secret=s3cret" in path.read_text()
    assert "Port 2200" in (tmp_path / "etc/ssh/sshd_config.d/10-tarvos.conf").read_text()


def test_configure_without_tacacs(tmp_path):
    configure({}, tmp_path, set_password=False)
    assert "pam_tacplus" not in (tmp_path / "etc/pam.d/sshd").read_text()
    assert not (tmp_path / "etc/libnss-ato.conf").exists()


def test_secret_never_logged(tmp_path, capsys):
    env = {"TACACS_SERVER": "10.0.0.1", "TACACS_SECRET": "topsecret"}
    configure(env, tmp_path, set_password=False)
    captured = capsys.readouterr()
    assert "topsecret" not in captured.out + captured.err
