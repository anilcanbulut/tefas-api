"""Configuration is reachable from both surfaces, and stays that way.

The rule this file enforces: anything tunable on ``Tefas(...)`` is also tunable
from the ``tefas`` command. Adding a constructor argument without a flag is the
easy mistake, so parity is asserted by introspection rather than by a list
someone has to remember to update.
"""

import inspect

import pytest

from tefas.cli import _client, build_parser
from tefas.client import Tefas
from tefas.ratelimit import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_RATE,
    backoff_delay,
)

#: Constructor arguments that deliberately have no flag: they take objects a
#: shell cannot express.
NOT_FLAGGABLE = {"self", "session", "progress"}


def constructor_knobs():
    return {
        name
        for name in inspect.signature(Tefas.__init__).parameters
        if name not in NOT_FLAGGABLE
    }


def parser_destinations():
    parser = build_parser()
    return {action.dest for action in parser._actions}


# -- parity ------------------------------------------------------------------


def test_every_python_knob_has_a_cli_flag():
    missing = constructor_knobs() - parser_destinations()
    assert not missing, "Tefas(...) arguments with no CLI flag: {}".format(
        ", ".join(sorted(missing))
    )


def test_the_cli_forwards_every_knob_it_parses():
    """A flag that parses but is never passed through is worse than no flag."""
    args = build_parser().parse_args(
        [
            "--rate", "2", "--window", "30", "--max-retries", "1",
            "--timeout", "15", "--backoff-base", "5", "--backoff-cap", "9",
            "--backoff-jitter", "0", "sync",
        ]
    )
    client = _client(args)

    assert client.limiter.rate == 2
    assert client.limiter.window == 30
    assert client.max_retries == 1
    assert client.timeout == 15
    assert client.backoff_base == 5
    assert client.backoff_cap == 9
    assert client.backoff_jitter == 0


def test_defaults_agree_between_cli_and_python():
    args = build_parser().parse_args(["sync"])
    default_client = Tefas()
    configured = _client(args)

    assert configured.limiter.rate == default_client.limiter.rate == DEFAULT_RATE
    assert configured.timeout == default_client.timeout
    assert configured.max_retries == default_client.max_retries
    assert configured.backoff_base == default_client.backoff_base


# -- the knobs actually do something -----------------------------------------


def test_backoff_settings_reach_the_delay_calculation():
    """Previously these existed on backoff_delay but were unreachable from
    Tefas(), so the 60s base could not be changed."""
    slept = []
    client = Tefas(backoff_base=5, backoff_cap=20, backoff_jitter=0.0)

    import tefas.client as module

    original = module.time.sleep
    module.time.sleep = slept.append
    try:
        client._sleep_for_retry(0)
        client._sleep_for_retry(1)
        client._sleep_for_retry(9)
    finally:
        module.time.sleep = original

    assert slept == [5.0, 10.0, 20.0], "base, doubling, then capped"


def test_rate_reaches_the_limiter():
    assert Tefas(rate=2, window=30).limiter.rate == 2
    assert Tefas(rate=2, window=30).limiter.window == 30


def test_backoff_defaults_are_unchanged_when_not_configured():
    client = Tefas()
    assert client.backoff_base == DEFAULT_BACKOFF_BASE
    assert client.backoff_cap == DEFAULT_BACKOFF_CAP
    assert backoff_delay(0, base=client.backoff_base, jitter=0.0) == DEFAULT_BACKOFF_BASE


def test_a_zero_or_negative_rate_is_rejected():
    with pytest.raises(ValueError):
        Tefas(rate=0)


# -- store and ingest knobs --------------------------------------------------


def test_cache_path_is_configurable(tmp_path):
    from tefas.store import Store

    path = tmp_path / "nested" / "custom.db"
    with Store(str(path)) as store:
        assert store.stats()["path"] == str(path)
    assert path.exists(), "parent directories should be created"


def test_db_flag_defaults_but_can_be_overridden():
    assert build_parser().parse_args(["stats"]).db == "tefas.db"
    assert build_parser().parse_args(["--db", "/data/x.db", "stats"]).db == "/data/x.db"


def test_sync_overlap_is_configurable():
    args = build_parser().parse_args(["sync", "--lookback", "3"])
    assert args.lookback == 3


def test_datasets_are_selectable():
    args = build_parser().parse_args(["backfill", "--datasets", "prices"])
    assert args.datasets == ["prices"]
