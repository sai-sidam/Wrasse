"""Tiny CLI expense tracker. Data lives in a JSON file.

Usage:
    python expenses.py add 12.50 "lunch"
    python expenses.py list
    python expenses.py total
"""
import argparse
import json
import os
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


def add(amount, description, path=DEFAULT_DB, on=None):
    items = load(path)
    item = {"amount": float(amount), "description": description,
            "date": (on or date.today()).isoformat()}
    items.append(item)
    save(items, path)
    return item


def list_expenses(path=DEFAULT_DB):
    return load(path)


def total(path=DEFAULT_DB):
    return round(sum(i["amount"] for i in load(path)), 2)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="expenses")
    parser.add_argument("--file", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("amount")
    p_add.add_argument("description")
    sub.add_parser("list")
    sub.add_parser("total")
    args = parser.parse_args(argv)

    if args.cmd == "add":
        item = add(args.amount, args.description, args.file)
        print(f"added {item['amount']:.2f} {item['description']}")
    elif args.cmd == "list":
        for i in list_expenses(args.file):
            print(f"{i['date']}  {i['amount']:>8.2f}  {i['description']}")
    elif args.cmd == "total":
        print(f"{total(args.file):.2f}")


if __name__ == "__main__":
    main()
