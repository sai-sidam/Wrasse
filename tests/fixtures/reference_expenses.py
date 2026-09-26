"""Reference solution for S1-S4 (used only to validate the hidden eval checks)."""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date

DEFAULT_DB = os.environ.get("EXPENSES_FILE", "expenses.json")


def load(path=DEFAULT_DB):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save(items, path=DEFAULT_DB):
    with open(path, "w") as f:
        json.dump(items, f, indent=2)


def add(amount, description, path=DEFAULT_DB, on=None, category="general"):
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError(f"amount must be a number, got {amount!r}")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not str(description).strip():
        raise ValueError("description is required")
    items = load(path)
    item = {"amount": amount, "description": description, "date": (on or date.today()).isoformat(),
            "category": category}
    items.append(item)
    save(items, path)
    return item


def list_expenses(path=DEFAULT_DB):
    return [{"category": "general", **i} for i in load(path)]


def total(path=DEFAULT_DB):
    return round(sum(i["amount"] for i in load(path)), 2)


def monthly_totals(path=DEFAULT_DB):
    out = defaultdict(float)
    for i in load(path):
        out[i["date"][:7]] += i["amount"]
    return {k: round(v, 2) for k, v in sorted(out.items())}


def export_csv(out_path, path=DEFAULT_DB):
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "amount", "description", "category"])
        w.writeheader()
        for i in list_expenses(path):
            w.writerow({k: i[k] for k in ("date", "amount", "description", "category")})


def main(argv=None):
    parser = argparse.ArgumentParser(prog="expenses")
    parser.add_argument("--file", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("amount")
    p_add.add_argument("description")
    p_add.add_argument("--category", default="general")
    sub.add_parser("list")
    sub.add_parser("total")
    sub.add_parser("report")
    p_exp = sub.add_parser("export")
    p_exp.add_argument("out")
    args = parser.parse_args(argv)
    if args.cmd == "add":
        try:
            item = add(args.amount, args.description, args.file, category=args.category)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"added {item['amount']:.2f} {item['description']}")
    elif args.cmd == "list":
        for i in list_expenses(args.file):
            print(f"{i['date']}  {i['amount']:>8.2f}  {i['category']:<10} {i['description']}")
    elif args.cmd == "total":
        print(f"{total(args.file):.2f}")
    elif args.cmd == "report":
        for month, amt in monthly_totals(args.file).items():
            print(f"{month}  {amt:>8.2f}")
    elif args.cmd == "export":
        export_csv(args.out, args.file)
        print(f"exported to {args.out}")


if __name__ == "__main__":
    main()
