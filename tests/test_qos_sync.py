import pytest
from qos_sync import BackendError, QosBackend, build, interface_state


def htb(**extra):
    return {"name": "eth1", "qdisc": "htb", "bandwidth": 10000, **extra}


def test_htb_classes_default_class_and_filters():
    commands = build(htb(**{"class": [
        {"id": 10, "dscp": [46], "priority": 0, "rate": 2000},
        {"id": 20, "dscp": [26, 28]},
    ]}))  # fmt: skip
    text = [" ".join(c) for c in commands]
    assert text[0] == "tc qdisc add dev eth1 root handle 1: htb default fff"
    root = "tc class add dev eth1 parent 1: classid 1:ffff htb rate 10000kbit ceil 10000kbit"
    assert root in text
    # id 10 -> classid 1:a (tc minors are hex)
    assert (
        "tc class add dev eth1 parent 1:ffff classid 1:a htb rate 2000kbit ceil 10000kbit prio 0"
        in text
    )
    # unset rate: equal share of what is left (8000 / 2 classes incl. the default one)
    assert any("classid 1:14 htb rate 4000kbit" in t for t in text)
    assert any("classid 1:fff htb rate 4000kbit ceil 10000kbit prio 7" in t for t in text)
    assert "tc qdisc add dev eth1 parent 1:a fq_codel" in text
    # DSCP 46 -> TOS 0xb8, IPv4 and IPv6
    assert any("protocol ip prio 1 u32 match ip dsfield 0xb8 0xfc flowid 1:a" in t for t in text)
    assert any("protocol ipv6 prio 2 u32 match ip6 priority 0xb8 0xfc flowid 1:a" in t
               for t in text)  # fmt: skip
    assert sum("flowid 1:14" in t for t in text) == 4


@pytest.mark.parametrize(
    "interface, message",
    [
        ({"name": "eth1", "qdisc": "htb"}, "needs the interface bandwidth"),
        (htb(**{"class": [{"id": 1, "rate": 6000}, {"id": 2, "rate": 6000}]}), "exceed"),
        (htb(**{"class": [{"id": 1, "rate": 3000, "ceil": 2000}]}), "above its ceil"),
        (htb(**{"class": [{"id": 1, "dscp": [10]}, {"id": 2, "dscp": [10]}]}), "DSCP 10"),
    ],
)
def test_htb_refusals(interface, message):
    with pytest.raises(BackendError, match=message):
        build(interface)


def test_prio_bands_and_simple_qdiscs():
    commands = build({"name": "eth1", "qdisc": "prio",
                      "class": [{"id": 1, "dscp": [46], "priority": 0}]})  # fmt: skip
    assert commands[0][:9] == ["tc", "qdisc", "add", "dev", "eth1", "root", "handle", "1:", "prio"]
    assert "flowid" in commands[1] and commands[1][-1] == "1:1"
    assert build({"name": "eth1", "qdisc": "fq-codel"}) == [
        ["tc", "qdisc", "add", "dev", "eth1", "root", "fq_codel"]
    ]
    assert build({"name": "eth1", "qdisc": "cake", "bandwidth": 5000})[0][-3:] == [
        "cake", "bandwidth", "5000kbit",
    ]  # fmt: skip
    assert build({"name": "eth1", "qdisc": "cake"})[0][-1] == "unlimited"


QDISCS = [{"kind": "htb", "handle": "1:", "root": True, "bytes": 1200, "packets": 12, "drops": 1,
           "overlimits": 3, "requeues": 0, "backlog": 0, "qlen": 0},
          {"kind": "fq_codel", "handle": "8001:", "parent": "1:a", "bytes": 1000,
           "packets": 10}]  # fmt: skip
CLASSES = [{"class": "htb", "handle": "1:1", "stats": {"bytes": 1200, "packets": 12}},
           {"class": "htb", "handle": "1:a", "stats": {"bytes": 1000, "packets": 10, "drops": 0}},
           {"class": "htb", "handle": "1:fff", "stats": {"bytes": 200, "packets": 2}}]  # fmt: skip


def test_interface_state():
    interface = htb(**{"class": [{"id": 10, "dscp": [46]}]})
    entry = interface_state(interface, QDISCS, CLASSES)
    assert entry["state"]["oper-status"] == "present"
    assert entry["state"]["root-qdisc"] == {
        "kind": "htb", "bytes": 1200, "packets": 12, "drops": 1, "overlimits": 3,
        "requeues": 0, "backlog-bytes": 0, "backlog-packets": 0,
    }  # fmt: skip
    assert entry["state"]["default-class"] == {"bytes": 200, "packets": 2}
    assert entry["class"] == [{"id": 10, "state": {"bytes": 1000, "packets": 10, "drops": 0}}]
    assert interface_state(interface, None, None)["state"] == {"oper-status": "not-present"}


def test_apply_rebuilds_only_on_change(monkeypatch, tmp_path):
    import qos_sync

    calls = []
    links = {"eth1": 5}
    monkeypatch.setattr(qos_sync, "ifindexes", lambda: dict(links))
    monkeypatch.setattr(qos_sync, "run", lambda command, check=True: calls.append(command))
    kinds = {"eth1": "fq_codel", "eth9": "fq_codel"}
    monkeypatch.setattr(qos_sync, "root_kind", lambda dev: kinds.get(dev))
    backend = QosBackend(owned_file=str(tmp_path / "owned.json"))
    config = {"qos": {"interface": [{"name": "eth1", "qdisc": "fq-codel"},
                                    {"name": "eth9", "qdisc": "fq-codel"}]}}  # fmt: skip
    backend.apply(config)
    assert calls == [
        ["tc", "qdisc", "del", "dev", "eth1", "root"],
        ["tc", "qdisc", "add", "dev", "eth1", "root", "fq_codel"],
    ]  # eth9 does not exist yet
    calls.clear()
    backend.apply(config)
    assert calls == []
    links["eth1"] = 6  # interface recreated: rebuilt
    links["eth9"] = 7  # appeared
    backend.apply(config)
    assert {c[4] for c in calls} == {"eth1", "eth9"}
    calls.clear()
    backend.apply({})  # removed from the configuration
    assert sorted(calls) == [
        ["tc", "qdisc", "del", "dev", "eth1", "root"],
        ["tc", "qdisc", "del", "dev", "eth9", "root"],
    ]


def test_class_id_1_does_not_collide_with_the_root_class():
    # v2.1.0 bug: htb's root class was 1:1, so <class><id>1</id> failed with "File exists"
    commands = build(htb(**{"class": [{"id": 1, "dscp": [46], "rate": 1000}]}))
    classids = [c[c.index("classid") + 1] for c in commands if "classid" in c]
    assert len(classids) == len(set(classids)) == 3  # root, class 1, default
    assert "1:1" in classids and "1:ffff" in classids


def test_failed_build_leaves_no_qdisc_and_orphans_are_removed(monkeypatch, tmp_path):
    """v2.1.0 bug: a refused edit left an htb root qdisc on the interface."""
    import qos_sync

    calls = []

    def run(command, check=True):
        calls.append(command)
        if command[:3] == ["tc", "class", "add"] and "1:a" in command:
            raise BackendError("RTNETLINK answers: File exists")

    monkeypatch.setattr(qos_sync, "ifindexes", lambda: {"eth1": 5})
    monkeypatch.setattr(qos_sync, "run", run)
    owned = str(tmp_path / "owned.json")
    backend = QosBackend(owned_file=owned)
    bad = {"qos": {"interface": [htb(**{"class": [{"id": 10, "rate": 1000}]})]}}
    with pytest.raises(BackendError):
        backend.apply(bad)
    # rolled back to the kernel default at once, nothing recorded as owned
    assert calls[-1] == ["tc", "qdisc", "del", "dev", "eth1", "root"]
    assert backend.owned == set() and backend.applied == {}

    # a daemon killed mid-build: the owned file survives, the next application (of a
    # configuration without eth1) removes the orphaned root qdisc
    calls.clear()
    with open(owned, "w") as handle:
        handle.write('["eth1"]')
    restarted = QosBackend(owned_file=owned)
    restarted.apply({})
    assert calls == [["tc", "qdisc", "del", "dev", "eth1", "root"]]
    assert restarted.owned == set()


def test_qdisc_removed_behind_tarvos_is_rebuilt(monkeypatch, tmp_path):
    # v2.1.1 chaos finding: "tc qdisc del dev eth1 root" was never repaired
    import qos_sync

    calls, kind = [], {"eth1": "htb"}
    monkeypatch.setattr(qos_sync, "ifindexes", lambda: {"eth1": 5})
    monkeypatch.setattr(qos_sync, "run", lambda command, check=True: calls.append(command))
    monkeypatch.setattr(qos_sync, "root_kind", lambda dev: kind.get(dev))
    backend = QosBackend(owned_file=str(tmp_path / "owned.json"))
    config = {"qos": {"interface": [htb()]}}
    backend.apply(config)
    calls.clear()
    backend.apply(config)
    assert calls == []  # in place: nothing to do
    kind["eth1"] = "noqueue"
    backend.apply(config)
    assert any(c[:6] == ["tc", "qdisc", "add", "dev", "eth1", "root"] for c in calls)
