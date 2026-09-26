"""S2: monthly total report."""
from datetime import date

import expenses


def test_monthly_totals(tmp_path):
    db = tmp_path / "e.json"
    expenses.add(10, "a", db, on=date(2026, 8, 3))
    expenses.add(2.5, "b", db, on=date(2026, 8, 30))
    expenses.add(4, "c", db, on=date(2026, 9, 1))
    assert expenses.monthly_totals(db) == {"2026-08": 12.5, "2026-09": 4.0}


def test_report_cli(tmp_path, capsys):
    db = str(tmp_path / "e.json")
    expenses.add(7, "x", db, on=date(2026, 9, 2))
    expenses.main(["--file", db, "report"])
    out = capsys.readouterr().out
    assert "2026-09" in out and "7.00" in out
