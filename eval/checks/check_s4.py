"""S4: input validation."""
import pytest

import expenses


@pytest.mark.parametrize("amount", [0, -3, "abc", ""])
def test_bad_amount(tmp_path, amount):
    with pytest.raises(ValueError):
        expenses.add(amount, "x", tmp_path / "e.json")


def test_empty_description(tmp_path):
    with pytest.raises(ValueError):
        expenses.add(5, "   ", tmp_path / "e.json")


def test_nothing_saved_on_error(tmp_path):
    db = tmp_path / "e.json"
    with pytest.raises(ValueError):
        expenses.add(-1, "x", db)
    assert expenses.list_expenses(db) == []


def test_cli_rejects_bad_amount(tmp_path):
    # `sys.exit(main())` is the entry point, so a non-zero return exits non-zero just like SystemExit
    try:
        code = expenses.main(["--file", str(tmp_path / "e.json"), "add", "abc", "x"])
    except SystemExit as e:
        code = e.code
    assert code not in (0, None)
