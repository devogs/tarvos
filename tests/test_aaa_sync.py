import json

import aaa_event
import pytest
from aaa_configure import AaaSettings, render_pam
from aaa_sync import AaaBackend, BackendError, effective, read_events, state

BASE = AaaSettings(("10.0.0.1", "10.0.0.2"), "envkey", 3)


def test_environment_is_used_until_servers_are_configured():
    settings, source = effective(BASE, {})
    assert source == "environment" and settings.servers == BASE.servers
    settings, source = effective(BASE, {"aaa": {"tacacs": {"timeout": 7}}})
    assert settings.timeout == 7 and settings.secret == "envkey"


def test_netconf_servers_replace_the_environment_with_their_own_secrets():
    config = {"aaa": {"tacacs": {"server": [
        {"address": "192.0.2.1", "secret": "k1"}, {"address": "2001:DB8::1", "secret": "k2"},
    ]}}}  # fmt: skip
    settings, source = effective(BASE, config)
    assert source == "netconf"
    assert settings.servers == ("192.0.2.1", "2001:db8::1")
    pam = render_pam(settings, with_session=False)
    assert "server=192.0.2.1 secret=k1 server=2001:db8::1 secret=k2" in pam


def test_same_secret_keeps_the_classic_argument_order():
    settings = AaaSettings(("192.0.2.1", "192.0.2.2"), "", 3, ("k", "k"))
    assert "server=192.0.2.1 server=192.0.2.2 secret=k " in render_pam(settings, with_session=False)


def test_disabling_local_fallback():
    with pytest.raises(BackendError, match="nobody could log in"):
        effective(AaaSettings((), "", 3), {"aaa": {"local-fallback": {"enabled": False}}})
    settings, _ = effective(BASE, {"aaa": {"local-fallback": {"enabled": False}}})
    pam = render_pam(settings, with_session=True)
    assert pam.splitlines()[1] == "auth requisite pam_succeed_if.so quiet user != admin"
    assert "account requisite pam_succeed_if.so quiet user != admin" in pam


def test_pam_stack_records_outcomes_and_only_jumps_on_success():
    pam = render_pam(BASE, with_session=False)
    auth = [line for line in pam.splitlines() if line.startswith("auth")]
    assert "aaa_event.py tacacs failure" in auth[2] and "aaa_event.py tacacs success" in auth[4]
    assert auth[-1].endswith("aaa_event.py local success")
    for line in auth:
        if "[" in line:  # a jump on failure would count as "ok" and break the stack
            assert "default=ignore" in line
    # admin skips straight to pam_unix, past the five TACACS+ lines
    assert auth[0].startswith("auth [success=5 ") and "pam_unix.so" in auth[6]


def test_state_and_active_server():
    probes = {"10.0.0.1": {"reachable": False, "rtt": None, "time": 0},
              "10.0.0.2": {"reachable": True, "rtt": 4, "time": 60}}  # fmt: skip
    events = {"tacacs": {"method": "tacacs", "result": "success", "user": "alice",
                         "service": "sshd", "rhost": "172.17.0.1", "time": 120}}  # fmt: skip
    data = state(BASE, "environment", probes, events)["aaa"]["state"]
    assert data["tacacs-active"] is True and data["active-servers"] == 2
    assert data["active-server"] == "10.0.0.2"
    assert data["server"][0] == {"address": "10.0.0.1", "priority": 1, "reachable": False,
                                 "last-probe": "1970-01-01T00:00:00Z"}  # fmt: skip
    assert data["server"][1]["response-time"] == 4
    assert data["last-tacacs-authentication"] == {
        "result": "success", "user": "alice", "service": "sshd", "remote-host": "172.17.0.1",
        "time": "1970-01-01T00:02:00Z",
    }  # fmt: skip
    assert "last-local-authentication" not in data
    assert "active-server" not in state(BASE, "environment", {}, {})["aaa"]["state"]


def test_event_recording(tmp_path, monkeypatch):
    env = {"PAM_TYPE": "auth", "PAM_USER": "alice", "PAM_SERVICE": "sshd",
           "PAM_RHOST": "10.1.1.1"}  # fmt: skip
    record = aaa_event.event("tacacs", "failure", env, 99.5)
    assert record == {"method": "tacacs", "result": "failure", "time": 99, "user": "alice",
                      "service": "sshd", "rhost": "10.1.1.1"}  # fmt: skip
    # values from the client are only kept when they are short and plain
    assert "user" not in aaa_event.event("local", "failure", {"PAM_USER": "a b\n"}, 0)
    aaa_event.write(str(tmp_path), "tacacs", record)
    assert json.loads((tmp_path / "tacacs.json").read_text()) == record
    assert read_events(str(tmp_path)) == {"tacacs": record}
    # main() ignores non-auth phases and bad arguments, and never fails
    monkeypatch.setattr(aaa_event, "STATE_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("PAM_TYPE", "account")
    assert aaa_event.main(["aaa_event.py", "tacacs", "success"]) == 0
    assert not (tmp_path / "x").exists()
    assert aaa_event.main(["aaa_event.py"]) == 0


def test_backend_writes_files_once(tmp_path, monkeypatch):
    import aaa_sync

    monkeypatch.setattr(aaa_sync.threading, "Thread", lambda **kw: type("T", (), {
        "start": lambda self: None})())  # fmt: skip
    writes = []
    monkeypatch.setattr(aaa_sync, "write_auth_files", lambda s, root: writes.append(s))
    backend = AaaBackend(root=tmp_path, env={"TACACS_SERVER": "10.0.0.1", "TACACS_SECRET": "k"})
    backend.apply({})
    backend.apply({})
    assert len(writes) == 1 and backend.source == "environment"
    backend.apply({"aaa": {"tacacs": {"server": [{"address": "192.0.2.9", "secret": "z"}]}}})
    assert len(writes) == 2 and backend.state({})["aaa"]["state"]["server-source"] == "netconf"


def test_failed_write_is_retried_by_the_restore(tmp_path, monkeypatch):
    import aaa_sync

    monkeypatch.setattr(aaa_sync.threading, "Thread", lambda **kw: type("T", (), {
        "start": lambda self: None})())  # fmt: skip
    writes = []

    def write(settings, root):
        if settings.servers == ("192.0.2.9",):
            raise OSError("No space left on device")
        writes.append(settings)

    monkeypatch.setattr(aaa_sync, "write_auth_files", write)
    backend = AaaBackend(root=tmp_path, env={"TACACS_SERVER": "10.0.0.1", "TACACS_SECRET": "k"})
    backend.apply({})
    with pytest.raises(OSError):
        backend.apply({"aaa": {"tacacs": {"server": [{"address": "192.0.2.9", "secret": "z"}]}}})
    backend.apply({})  # restore of the running configuration: must rewrite, not skip
    assert len(writes) == 2
