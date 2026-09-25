"""A usable partial board is enough to register an employer, not claim coverage."""
from unittest.mock import patch

import pytest

from venator.discover.adapters import PartialBoardError, PostingBatch
from venator.discover.register import Board, confirm


@pytest.mark.parametrize("raise_partial", [False, True])
@pytest.mark.parametrize("rows", [[], [{"key": "workday:fictional.wd1~Careers:R1"}]])
def test_partial_registration_retains_presence_and_lower_bound_count(rows, raise_partial):
    def fetch(_board):
        if raise_partial:
            raise PartialBoardError("Bounded source stopped", rows)
        return PostingBatch(rows, complete=False, status="partial")
    with patch.dict("venator.discover.register.ADAPTERS", {"workday": fetch}):
        [result] = confirm([Board("workday", "fictional.wd1~Careers", "https://fictional.example", "pasted URL")])
    assert result.confirmed is bool(rows)
    assert result.posting_count == len(rows)
    assert result.complete is False


def test_complete_empty_direct_board_retains_valid_zero():
    with patch.dict("venator.discover.register.ADAPTERS", {"greenhouse": lambda _: []}):
        [result] = confirm([Board("greenhouse", "fictional", "https://fictional.example", "pasted URL")])
    assert result.confirmed is True
    assert result.complete is True
    assert result.posting_count == 0


def test_workday_registration_requests_one_bounded_window():
    batch = PostingBatch([{"key": "workday:fictional.wd1~Careers:R1"}], complete=False, status="partial")
    with patch("venator.discover.register.fetch_workday_window", return_value=batch) as probe:
        [result] = confirm([Board("workday", "fictional.wd1~Careers", "https://fictional.example", "pasted URL")])
    probe.assert_called_once_with("fictional.wd1~Careers", max_pages=1)
    assert result.confirmed is True
    assert result.complete is False
