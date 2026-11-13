import json

import pytest
from tarvos_frr.mgmtd import MgmtdError, collect_units, edit_commands, encode, to_dict_form


class _Node:
    def __init__(self, path, data, children=()):
        self._path, self._data, self._children = path, data, list(children)

    def path(self):
        return self._path

    def children(self):
        return iter(self._children)

    def print_mem(self, fmt, **kwargs):
        assert fmt == "json" and kwargs.get("pretty") is False
        return json.dumps(self._data, separators=(",", ":"))


class DContainer(_Node):  # named like libyang's class: units may descend into it
    pass


class DList(_Node):
    pass


def test_units_are_children_of_the_top_container():
    eth1 = DList("/frr-interface:lib/interface[name='eth1']", {"i": [{"name": "eth1", "d": "a b"}]})
    empty = DContainer("/frr-interface:lib/defaults", {})
    top = DContainer("/frr-interface:lib", {}, [eth1, empty])
    units = collect_units([top])
    assert units == {
        "/frr-interface:lib/interface[name='eth1']": '{"i":[{"name":"eth1","d":"a\\u0020b"}]}'
    }


def test_large_containers_are_split():
    small = DList("/m:top/c/l[k='1']", {"l": [{"k": "1"}]})
    big = DContainer("/m:top/c", {"c": {"l": [{"k": "1"}], "pad": "x" * 100}}, [small])
    top = DContainer("/m:top", {}, [big])
    assert list(collect_units([top], limit=50)) == ["/m:top/c/l[k='1']"]
    assert list(collect_units([top], limit=5000)) == ["/m:top/c"]


def test_edit_commands_replace_changed_remove_deleted():
    old = {"/m:a/x[k='1']": "{1}", "/m:a/x[k='2']": "{2}", "/m:a/y": "{y}"}
    new = {"/m:a/x[k='1']": "{1}", "/m:a/x[k='2']": "{22}", "/m:a/z": "{z}"}
    assert edit_commands(old, new) == [
        "mgmt edit remove /m:a/y",
        "mgmt edit replace /m:a/x[k='2'] json {22}",
        "mgmt edit replace /m:a/z json {z}",
    ]


def test_interface_and_vrf_entries_are_cleared_not_deleted():
    old = {
        "/frr-interface:lib/interface[name='lo']": "{x}",
        "/frr-vrf:lib/vrf[name='RED']": "{y}",
    }
    assert edit_commands(old, {}) == [
        "mgmt edit replace /frr-interface:lib/interface[name='lo'] json "
        '{"frr-interface:interface":[{"name":"lo"}]}',
        'mgmt edit replace /frr-vrf:lib/vrf[name=\'RED\'] json {"frr-vrf:vrf":[{"name":"RED"}]}',
    ]


def test_xpath_with_spaces_is_refused():
    with pytest.raises(MgmtdError):
        edit_commands({}, {"/m:a/x[k='a b']": "{}"})


def test_encode_keeps_json_valid():
    text = encode(json.dumps({"d": "two words"}, separators=(",", ":")))
    assert " " not in text and json.loads(text) == {"d": "two words"}


def test_to_dict_form_converts_empty_leaves():
    data = {"a": [{"fib": [None], "n": [1, 2]}], "b": [None]}
    assert to_dict_form(data) == {"a": [{"fib": None, "n": [1, 2]}], "b": None}
    assert to_dict_form(None) is None
