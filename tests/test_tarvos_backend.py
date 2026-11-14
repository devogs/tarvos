import inspect

import pytest
from tarvos_backend import BackendError, Runner, oper_callback


class FakeBackend:
    module, xpath, name, interval = "m", "/m:x", "fake", None

    def __init__(self, fail_on=None):
        self.applied = []
        self.fail_on = fail_on
        self.oper = [("/m:x/state", lambda config: {"x": {"state": config}})]

    def apply(self, config):
        if config == self.fail_on:
            raise BackendError("nope")
        self.applied.append(config)


def test_change_applies_and_remembers_config():
    backend = FakeBackend()
    runner = Runner(backend)
    runner.on_change("change", lambda: {"a": 1}, lambda: {})
    assert backend.applied == [{"a": 1}] and runner.config == {"a": 1}


def test_refused_change_restores_running():
    backend = FakeBackend(fail_on={"bad": 1})
    runner = Runner(backend)
    with pytest.raises(BackendError):
        runner.on_change("change", lambda: {"bad": 1}, lambda: {"good": 1})
    assert backend.applied == [{"good": 1}] and runner.config == {"good": 1}


def test_abort_restores_running():
    backend = FakeBackend()
    runner = Runner(backend)
    runner.on_change("abort", lambda: {"new": 1}, lambda: {"old": 1})
    assert backend.applied == [{"old": 1}]


def test_oper_callback_signature_and_errors():
    backend = FakeBackend()
    runner = Runner(backend)
    runner.config = {"c": 1}
    callback = oper_callback(runner, backend.oper[0][1])
    # sysrepo-python requires a plain function with exactly two positional parameters.
    assert inspect.isfunction(callback) and len(inspect.signature(callback).parameters) == 2
    assert callback("/m:x/state", None) == {"x": {"state": {"c": 1}}}

    def broken(config):
        raise RuntimeError("boom")

    assert oper_callback(runner, broken)("/x", None) is None


def test_transaction_in_flight_until_done_or_abort():
    runner = Runner(FakeBackend())
    runner.on_change("change", lambda: {"a": 1}, lambda: {})
    assert runner.in_flight is not None
    runner.on_change("done", lambda: {}, lambda: {})
    assert runner.in_flight is None
    runner.on_change("change", lambda: {"a": 1}, lambda: {})
    runner.on_change("abort", lambda: {}, lambda: {})
    assert runner.in_flight is None
