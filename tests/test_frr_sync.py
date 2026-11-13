import pytest
from frr_sync import FrrSync
from tarvos_frr.renderers import ConfigError
from tarvos_frr.renderers.bgp import BgpRenderer
from tarvos_frr.renderers.ospf import OspfRenderer
from tarvos_frr.vtysh import FrrError

RUNNING = "frr version 10.7.1\n!\ninterface eth1\n description x\nexit\n!\nend\n"


class FakeFrr:
    def __init__(self, daemons=("mgmtd", "zebra", "bgpd", "ospfd")):
        self.daemons = set(daemons)
        self.reloads: list[str] = []
        self.fail_reload = False

    def running_daemons(self):
        return self.daemons

    def running_config(self):
        return RUNNING

    def check_config(self, text):
        pass

    def reload(self, text):
        self.reloads.append(text)
        if self.fail_reload:
            raise FrrError("% Unknown command")

    def mgmt_transaction(self, commands):
        raise AssertionError("no native data in these tests")


class FakeSession:
    def __init__(self, data):
        self.data = data

    def get_data(self, xpath):
        return self.data.get(xpath, {})


BGP = {"/tarvos-bgp:bgp": {"bgp": {"instance": [{"vrf": "default", "asn": 65000}]}}}
OSPF = {
    "/tarvos-ospf:ospf": {"ospf": {"interface": [{"name": "eth1", "area": "0.0.0.0"}]}},
}


def make_sync(frr):
    sync = FrrSync([BgpRenderer(), OspfRenderer()], frr, read=lambda s, xpath: s.get_data(xpath))
    sync.sync_native = lambda session, modules=None: False  # native path tested elsewhere
    return sync


def test_apply_reloads_once_and_is_idempotent():
    frr = FakeFrr()
    sync = make_sync(frr)
    sync.apply(FakeSession({**BGP, **OSPF}))
    assert len(frr.reloads) == 1
    assert "router bgp 65000\nexit" in frr.reloads[0]
    assert "interface eth1\n description x\n ip ospf area 0.0.0.0\nexit" in frr.reloads[0]
    sync.apply(FakeSession({**BGP, **OSPF}))
    assert len(frr.reloads) == 1


def test_event_only_rerenders_its_module():
    frr = FakeFrr()
    sync = make_sync(frr)
    sync.apply(FakeSession({**BGP, **OSPF}))
    # A tarvos-bgp event session shows tarvos-ospf as committed (here: empty). The OSPF
    # part must come from what was applied last, not be removed.
    sync.apply(FakeSession({"/tarvos-bgp:bgp": {"bgp": {"instance": []}}}), {"tarvos-bgp"})
    last = frr.reloads[-1]
    assert "router bgp" not in last
    assert " ip ospf area 0.0.0.0" in last


def test_missing_daemon_is_refused_before_touching_frr():
    frr = FakeFrr(daemons=("mgmtd", "zebra"))
    with pytest.raises(ConfigError, match="bgpd"):
        make_sync(frr).apply(FakeSession(BGP))
    assert frr.reloads == []


def test_failed_reload_keeps_ownership_for_resync():
    frr = FakeFrr()
    sync = make_sync(frr)
    frr.fail_reload = True
    with pytest.raises(FrrError):
        sync.apply(FakeSession(BGP))
    frr.fail_reload = False
    sync.resync(FakeSession({}))  # committed datastore has no BGP
    assert "router bgp" not in frr.reloads[-1]
    assert sync.rendered["tarvos-bgp"].empty and not sync.dirty
