import pytest
from lldp_configure import load_settings, render


def test_defaults_enable_lldp_on_all_interfaces():
    settings = load_settings({}, hostname="r1")
    conf = render(settings)
    assert settings.enabled
    assert "configure system hostname r1" in conf
    assert 'configure system interface pattern "*"' in conf
    assert "configure lldp tx-interval 30" in conf
    assert "configure lldp tx-hold 4" in conf


def test_custom_values():
    settings = load_settings(
        {
            "LLDP_INTERFACES": "eth*,!eth0",
            "LLDP_TX_INTERVAL": "10",
            "LLDP_TX_HOLD": "3",
            "LLDP_SYSTEM_NAME": "leaf1",
            "LLDP_SYSTEM_DESCRIPTION": "Tarvos leaf (lab)",
        }
    )
    conf = render(settings)
    assert 'pattern "eth*,!eth0"' in conf
    assert "tx-interval 10" in conf
    assert "hostname leaf1" in conf
    assert 'description "Tarvos leaf (lab)"' in conf


def test_can_be_disabled():
    assert not load_settings({"LLDP_ENABLED": "no"}).enabled


@pytest.mark.parametrize(
    "env",
    [
        {"LLDP_INTERFACES": 'eth0"\nconfigure system hostname pwned'},
        {"LLDP_SYSTEM_NAME": "a b"},
        {"LLDP_SYSTEM_DESCRIPTION": 'x"\nconfigure lldp tx-interval 1'},
        {"LLDP_TX_INTERVAL": "0"},
        {"LLDP_TX_HOLD": "abc"},
        {"LLDP_ENABLED": "maybe"},
    ],
)
def test_invalid_input_disables_lldp(env):
    assert not load_settings(env).enabled


def test_system_name_defaults_to_the_hostname(monkeypatch):
    # v2.1.0 bug: lldp_sync built its settings without the hostname and re-applied
    # "tarvos" on every node
    import lldp_configure

    monkeypatch.setattr(lldp_configure.socket, "gethostname", lambda: "RTR-2.lab.example")
    assert load_settings({}).system_name == "RTR-2"
    monkeypatch.setattr(lldp_configure.socket, "gethostname", lambda: "leaf 1!")
    assert load_settings({}).system_name == "leaf-1-"
    monkeypatch.setattr(lldp_configure.socket, "gethostname", lambda: "")
    assert load_settings({}).system_name == "tarvos"
    assert load_settings({"LLDP_SYSTEM_NAME": "spine1"}).system_name == "spine1"


def test_runtime_backend_uses_the_same_default(monkeypatch):
    import lldp_configure
    import lldp_sync

    monkeypatch.setattr(lldp_configure.socket, "gethostname", lambda: "RTR-3")
    assert lldp_sync.Agent(session=None).base.system_name == "RTR-3"
