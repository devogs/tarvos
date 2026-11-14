import pytest
from nft_sync import BackendError, counters, render, render_rule, state


def config(*rules, enabled=True):
    return {"nftables": {"copp": {"enabled": enabled, "rule": list(rules)}}}


BGP = {"name": "bgp", "protocol": "tcp", "destination-port": 179,
       "source-prefix": "10.0.0.0/8", "action": "accept"}  # fmt: skip
SSH = {"name": "ssh", "protocol": "tcp", "destination-port": 22, "action": "rate-limit",
       "max-pps": 200}  # fmt: skip
ICMP6 = {"name": "no-icmp6", "protocol": "icmpv6", "source-prefix": "2001:db8::/32",
         "action": "drop"}  # fmt: skip


def test_render_replaces_the_tarvos_table_atomically():
    text = render(config(BGP, SSH))
    lines = text.splitlines()
    assert lines[:2] == ["table inet tarvos", "delete table inet tarvos"]
    assert "type filter hook input priority filter; policy accept;" in text
    assert (
        'ip saddr 10.0.0.0/8 meta l4proto 6 th dport 179 counter accept comment "tarvos:bgp"'
        in text
    )
    # rate-limit: the excess is dropped first, the rest accepted
    assert text.index("limit rate over 200/second burst 20 packets") < text.index(
        'counter accept comment "tarvos:ssh"'
    )


def test_render_without_rules_or_disabled_only_removes_the_table():
    for cfg in ({}, config(), config(BGP, enabled=False)):
        assert render(cfg) == "table inet tarvos\ndelete table inet tarvos\n"


def test_ipv6_prefix_and_protocol_numbers():
    (rule,) = render_rule(ICMP6)
    assert rule.startswith("ip6 saddr 2001:db8::/32 meta l4proto 58 counter drop")
    (ospf,) = render_rule({"name": "ospf", "protocol": "ospf"})
    assert "meta l4proto 89" in ospf and "th dport" not in ospf


def test_burst_default_has_a_floor():
    rules = render_rule({**SSH, "max-pps": 10})
    assert "burst 5 packets" in rules[0]
    assert "burst 7 packets" in render_rule({**SSH, "burst": 7})[0]


def test_invalid_rule_name_is_refused():
    with pytest.raises(BackendError):
        render_rule({**BGP, "name": 'x" ; flush ruleset ; "'})


def _listing(*rules):
    items = [{"table": {"family": "inet", "name": "tarvos"}}]
    for comment, packets, nbytes in rules:
        items.append({"rule": {"family": "inet", "table": "tarvos", "chain": "copp",
                               "comment": comment,
                               "expr": [{"match": {}}, {"counter": {"packets": packets,
                                                                    "bytes": nbytes}},
                                        {"accept": None}]}})  # fmt: skip
    return {"nftables": items}


def test_counters_and_state():
    listing = _listing(
        ("tarvos:bgp", 10, 1000), ("tarvos:ssh:excess", 3, 300), ("tarvos:ssh", 7, 700),
        ("tarvos:no-icmp6", 2, 200),
    )  # fmt: skip
    assert counters(listing)["tarvos:ssh:excess"] == {"packets": 3, "bytes": 300}
    data = state(config(BGP, SSH, ICMP6), listing)["nftables"]["copp"]
    assert data["state"] == {"active": True, "rule-count": 3}
    rules = {r["name"]: r["state"] for r in data["rule"]}
    assert rules["bgp"] == {"matched-packets": 10, "matched-bytes": 1000,
                            "dropped-packets": 0, "dropped-bytes": 0}  # fmt: skip
    assert rules["ssh"]["matched-packets"] == 10 and rules["ssh"]["dropped-packets"] == 3
    assert rules["no-icmp6"]["dropped-packets"] == 2


def test_state_when_inactive():
    data = state(config(BGP, enabled=False), None)["nftables"]["copp"]
    assert data == {"state": {"active": False, "rule-count": 0}}


def test_every_failure_path_leaves_the_ruleset_alone(monkeypatch):
    """Milestone 2, bug 3: no failure may leave a partly applied CoPP table."""
    import nft_sync

    calls = []
    config = {"nftables": {"copp": {"rule": [
        {"name": "bgp", "protocol": "tcp", "destination-port": 179}]}}}  # fmt: skip

    def nft(*args, script=None):
        calls.append(args)
        if args[0] == fail_on:
            raise nft_sync.BackendError("nft: Error: syntax error")
        return ""

    monkeypatch.setattr(nft_sync, "nft", nft)
    backend = nft_sync.NftBackend()
    # 1. render refuses: nft is never called
    with pytest.raises(nft_sync.BackendError):
        fail_on = None
        backend.apply({"nftables": {"copp": {"rule": [{"name": "bad name", "protocol": "tcp"}]}}})
    assert calls == []
    # 2. the dry run refuses: the real transaction is never sent
    fail_on = "-c"
    with pytest.raises(nft_sync.BackendError):
        backend.apply(config)
    assert calls == [("-c", "-f", "-")]
    # 3. the transaction itself fails: one "nft -f" is all-or-nothing in the kernel, and it
    #    is the only command sent
    calls.clear()
    fail_on = "-f"
    with pytest.raises(nft_sync.BackendError):
        backend.apply(config)
    assert calls == [("-c", "-f", "-"), ("-f", "-")]
