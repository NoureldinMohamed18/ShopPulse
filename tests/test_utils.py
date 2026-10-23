import numpy as np
import pytest

from shoppulse.utils import retry, row_hash


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    @retry(attempts=3, delay=0, exceptions=(ValueError,))
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_gives_up_and_raises():
    calls = {"n": 0}

    @retry(attempts=2, delay=0, exceptions=(ValueError,))
    def always_fails():
        calls["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        always_fails()
    assert calls["n"] == 2


def test_retry_does_not_catch_other_exceptions():
    calls = {"n": 0}

    @retry(attempts=5, delay=0, exceptions=(ValueError,))
    def wrong_type():
        calls["n"] += 1
        raise KeyError("different")

    with pytest.raises(KeyError):
        wrong_type()
    assert calls["n"] == 1


def test_row_hash_is_stable_and_type_tolerant():
    assert row_hash("a", 3) == row_hash("a", 3.0) == row_hash(" a ", np.int64(3))
    assert row_hash("a", None) == row_hash("a", float("nan"))
    assert row_hash("a", 1) != row_hash("a", 2)
