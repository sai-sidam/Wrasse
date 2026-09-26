"""S3: CSV export."""
import csv
from datetime import date

import expenses


def test_export_csv(tmp_path):
    db = tmp_path / "e.json"
    out = tmp_path / "out.csv"
    expenses.add(10, "lunch", db, on=date(2026, 9, 1))
    expenses.export_csv(out, db)
    rows = list(csv.DictReader(open(out, newline="")))
    assert list(rows[0].keys())[:3] == ["date", "amount", "description"]
    assert rows[0]["date"] == "2026-09-01" and float(rows[0]["amount"]) == 10 and rows[0]["description"] == "lunch"


def test_export_cli(tmp_path):
    db = str(tmp_path / "e.json")
    out = tmp_path / "x.csv"
    expenses.add(1, "a", db)
    expenses.main(["--file", db, "export", str(out)])
    assert out.exists() and "description" in out.read_text()
