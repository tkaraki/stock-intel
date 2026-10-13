import pytest

from portfolio_diff import ChangeType, HoldingChange, diff_holdings


def only(prior, current):
    result = diff_holdings(prior, current)
    assert len(result) == 1
    return result[0]


@pytest.mark.parametrize("shares", [1, 1_000_000])
def test_new_position_flagged_regardless_of_size(shares):
    change = only({}, {"AAPL": shares})
    assert change == HoldingChange("AAPL", ChangeType.NEW, 0.0, shares, None)


def test_prior_zero_counts_as_new():
    assert only({"AAPL": 0}, {"AAPL": 5}).change_type is ChangeType.NEW


def test_full_exit_flagged():
    change = only({"AAPL": 100}, {})
    assert change.change_type is ChangeType.EXIT
    assert change.pct_change == -1.0
    assert only({"AAPL": 100}, {"AAPL": 0}).change_type is ChangeType.EXIT


@pytest.mark.parametrize(
    ("prior", "current", "expected"),
    [
        (100, 126, ChangeType.INCREASED),
        (100, 125, ChangeType.INCREASED),
        (80, 100, ChangeType.INCREASED),
        (100, 75, ChangeType.DECREASED),
        (100, 50, ChangeType.DECREASED),
    ],
)
def test_material_changes_flagged(prior, current, expected):
    change = only({"X": prior}, {"X": current})
    assert change.change_type is expected
    assert change.pct_change == pytest.approx((current - prior) / prior)


@pytest.mark.parametrize(("prior", "current"), [(100, 124), (100, 100), (100, 76)])
def test_immaterial_changes_ignored(prior, current):
    assert diff_holdings({"X": prior}, {"X": current}) == []


def test_empty_prior_all_new():
    result = diff_holdings({}, {"B": 1, "A": 2})
    assert [c.ticker for c in result] == ["A", "B"]
    assert all(c.change_type is ChangeType.NEW for c in result)


def test_empty_current_all_exit():
    result = diff_holdings({"B": 1, "A": 2}, {})
    assert [c.ticker for c in result] == ["A", "B"]
    assert all(c.change_type is ChangeType.EXIT for c in result)


def test_both_zero_or_empty_yields_nothing():
    assert diff_holdings({}, {}) == []
    assert diff_holdings({"A": 0}, {"A": 0}) == []


def test_negative_shares_raise():
    with pytest.raises(ValueError):
        diff_holdings({"A": -1}, {})
    with pytest.raises(ValueError):
        diff_holdings({}, {"A": -1})
