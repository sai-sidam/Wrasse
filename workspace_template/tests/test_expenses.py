from datetime import date

import expenses


def test_add_and_list(tmp_path):
    db = tmp_path / "e.json"
    expenses.add(12.5, "lunch", db)
    items = expenses.list_expenses(db)
    assert items == [{"amount": 12.5, "description": "lunch", "date": date.today().isoformat()}]


def test_total(tmp_path):
    db = tmp_path / "e.json"
    expenses.add(10, "a", db)
    expenses.add(2.25, "b", db)
    assert expenses.total(db) == 12.25


def test_empty_total(tmp_path):
    assert expenses.total(tmp_path / "missing.json") == 0


def test_cli_total(tmp_path, capsys):
    db = str(tmp_path / "e.json")
    expenses.main(["--file", db, "add", "3", "coffee"])
    expenses.main(["--file", db, "total"])
    assert capsys.readouterr().out.strip().endswith("3.00")
