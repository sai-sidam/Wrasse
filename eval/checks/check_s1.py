"""S1: add a category field."""
import expenses


def test_category_stored(tmp_path):
    db = tmp_path / "e.json"
    item = expenses.add(5, "salad", db, category="food")
    assert item["category"] == "food"
    assert expenses.list_expenses(db)[0]["category"] == "food"


def test_category_defaults_to_general(tmp_path):
    db = tmp_path / "e.json"
    expenses.add(5, "misc", db)
    assert expenses.list_expenses(db)[0]["category"] == "general"


def test_cli_category(tmp_path, capsys):
    db = str(tmp_path / "e.json")
    expenses.main(["--file", db, "add", "3", "coffee", "--category", "drinks"])
    assert expenses.list_expenses(db)[0]["category"] == "drinks"
