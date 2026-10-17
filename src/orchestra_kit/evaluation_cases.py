"""Self-contained programming fixtures used by the stage-one evaluator.

Each fixture deliberately contains only public synthetic code and its isolated
unittest grader.  The evaluator writes ``files`` into a temporary workspace,
allows edits only at ``write_paths``, and runs ``tests`` with Python isolation.
"""

from __future__ import annotations


CASES = {
    "expense-csv-decimals": {
        "category": "bugfix",
        "goal": "Fix the CSV expense loader so quoted fields and decimal money are handled safely.",
        "acceptance": [
            "Correctly parse CSV rows with quoted commas in the description field.",
            "Return Decimal amounts and reject malformed, negative, or over-precise money values; zero is valid.",
            "For malformed money or column counts, raise ExpenseFormatError whose message identifies the row number and a short reason.",
        ],
        "files": {
            "expense_loader.py": '''import csv
from decimal import Decimal


def load_expenses(path):
    expenses = []
    with open(path, encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            description, category, amount = line.strip().split(",")
            expenses.append({"description": description, "category": category, "amount": Decimal(amount)})
    return expenses
''',
            "expenses.csv": '''description,category,amount
"Team, lunch",meals,12.50
Taxi,travel,18.00
''',
        },
        "solution": {
            "expense_loader.py": '''import csv
from decimal import Decimal, InvalidOperation


class ExpenseFormatError(ValueError):
    pass


def load_expenses(path):
    expenses = []
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != ["description", "category", "amount"]:
            raise ExpenseFormatError("expected description,category,amount header")
        for line_number, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != 3:
                raise ExpenseFormatError("row %d must have three columns" % line_number)
            description, category, amount_text = (value.strip() for value in row)
            if not description or not category:
                raise ExpenseFormatError("row %d needs description and category" % line_number)
            try:
                amount = Decimal(amount_text)
            except InvalidOperation as error:
                raise ExpenseFormatError("row %d has invalid amount" % line_number) from error
            if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
                raise ExpenseFormatError("row %d has invalid amount" % line_number)
            expenses.append({"description": description, "category": category, "amount": amount.quantize(Decimal("0.01"))})
    return expenses
''',
        },
        "tests": '''import pathlib
import sys
import tempfile
import unittest
from decimal import Decimal

workspace = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from expense_loader import ExpenseFormatError, load_expenses


class ExpenseLoaderTests(unittest.TestCase):
    def test_quoted_description_and_decimal_amount_are_preserved(self):
        rows = load_expenses(workspace / "expenses.csv")
        self.assertEqual(rows[0], {"description": "Team, lunch", "category": "meals", "amount": Decimal("12.50")})
        self.assertEqual(rows[1]["amount"], Decimal("18.00"))

    def test_zero_amount_is_valid_decimal_money(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "zero.csv"
            for amount in ("0", "0.00", "-0", "-0.00"):
                with self.subTest(amount=amount):
                    path.write_text("description,category,amount\\nCredit,adjustment," + amount + "\\n", encoding="utf-8")
                    rows = load_expenses(path)
                    self.assertEqual(rows, [{"description": "Credit", "category": "adjustment", "amount": Decimal("0.00")}])
                    self.assertIsInstance(rows[0]["amount"], Decimal)

    def test_bad_money_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bad.csv"
            path.write_text("description,category,amount\\nCoffee,meals,12.345\\n", encoding="utf-8")
            with self.assertRaises(ExpenseFormatError) as captured:
                load_expenses(path)
            message = str(captured.exception).lower()
            self.assertRegex(message, r"(?<![0-9])2(?![0-9])")
            self.assertTrue(any(char.isalpha() for char in message.replace("row", "").replace("line", "").replace("record", "")))

    def test_negative_and_non_numeric_money_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bad.csv"
            for amount in ("-1.00", "twelve", "NaN"):
                path.write_text("description,category,amount\\nCoffee,meals," + amount + "\\n", encoding="utf-8")
                with self.assertRaises(ExpenseFormatError) as captured:
                    load_expenses(path)
                message = str(captured.exception).lower()
                self.assertRegex(message, r"(?<![0-9])2(?![0-9])")
                self.assertTrue(any(char.isalpha() for char in message.replace("row", "").replace("line", "").replace("record", "")))

    def test_wrong_column_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bad.csv"
            path.write_text("description,category,amount\\nCoffee,meals,2.00,extra\\n", encoding="utf-8")
            with self.assertRaises(ExpenseFormatError) as captured:
                load_expenses(path)
            message = str(captured.exception).lower()
            self.assertRegex(message, r"(?<![0-9])2(?![0-9])")
            self.assertTrue(any(char.isalpha() for char in message.replace("row", "").replace("line", "").replace("record", "")))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
''',
        "write_paths": ["expense_loader.py"],
    },
    "archive-member-filter": {
        "category": "bugfix",
        "goal": "Make archive member filtering reject traversal and platform-specific absolute paths without extracting anything.",
        "acceptance": [
            "Expose is_safe_member(name) as a boolean predicate and safe_members(members) as the ordered filter.",
            "Keep ordinary relative POSIX member names in their original order.",
            "Reject parent traversal, absolute paths, empty path components, dot and dot-dot components, and Windows drive-prefixed paths such as C:/data and C:data.",
            "is_safe_member returns False for empty or non-string values.",
            "Treat backslashes as path separators before applying safety checks.",
        ],
        "files": {
            "archive_filter.py": '''def safe_members(members):
    return [name for name in members if not name.startswith("/")]
''',
        },
        "solution": {
            "archive_filter.py": '''import re


def is_safe_member(name):
    if not isinstance(name, str) or not name:
        return False
    normalized = name.replace("\\\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    parts = normalized.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def safe_members(members):
    return [name for name in members if is_safe_member(name)]
''',
        },
        "tests": '''import pathlib
import sys
import unittest

workspace = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from archive_filter import is_safe_member, safe_members


class ArchiveMemberFilterTests(unittest.TestCase):
    def test_keeps_safe_relative_names_in_order(self):
        names = ["docs/readme.txt", "images/logo.png", "nested/file.json"]
        self.assertEqual(safe_members(names), names)

    def test_filters_posix_traversal_and_absolute_names(self):
        names = ["docs/readme.txt", "../secret.txt", "docs/../../secret.txt", "/etc/passwd"]
        self.assertEqual(safe_members(names), ["docs/readme.txt"])

    def test_filters_windows_absolute_and_traversal_names(self):
        names = ["C:\\\\temp\\\\secret.txt", "C:relative.txt", "docs\\\\..\\\\secret.txt", "safe\\\\guide.txt"]
        self.assertEqual(safe_members(names), ["safe\\\\guide.txt"])

    def test_rejects_ambiguous_components_and_non_strings(self):
        self.assertFalse(is_safe_member("docs//guide.txt"))
        self.assertFalse(is_safe_member("./guide.txt"))
        self.assertFalse(is_safe_member("docs/../guide.txt"))
        self.assertFalse(is_safe_member("."))
        self.assertFalse(is_safe_member(".."))
        self.assertFalse(is_safe_member(""))
        self.assertFalse(is_safe_member(None))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
''',
        "write_paths": ["archive_filter.py"],
    },
    "ttl-cache-bounds": {
        "category": "feature",
        "goal": "Implement a bounded TTL cache with an injected clock so expiry is deterministic in tests.",
        "acceptance": [
            "Support set(key, value, ttl) and get(key, default=None) with expiration based on the injected clock; a value expires when clock() is at least its deadline.",
            "Capacity is a positive integer (not bool); invalid capacity and negative TTL values raise ValueError.",
            "Purge expired entries before using least-recently-used eviction when capacity is exceeded.",
        ],
        "files": {
            "ttl_cache.py": '''class TTLCache:
    def __init__(self, capacity, clock):
        self.capacity = capacity
        self.clock = clock
        self.items = {}

    def set(self, key, value, ttl):
        self.items[key] = value

    def get(self, key, default=None):
        return self.items.get(key, default)
''',
        },
        "solution": {
            "ttl_cache.py": '''from collections import OrderedDict
import time


class TTLCache:
    def __init__(self, capacity, clock=None):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.clock = clock or time.monotonic
        self.items = OrderedDict()

    def set(self, key, value, ttl):
        if ttl < 0:
            raise ValueError("ttl must not be negative")
        self._purge_expired()
        self.items.pop(key, None)
        self.items[key] = (value, self.clock() + ttl)
        self._purge_expired()
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)

    def get(self, key, default=None):
        record = self.items.get(key)
        if record is None:
            return default
        value, expires_at = record
        if self.clock() >= expires_at:
            del self.items[key]
            return default
        self.items.move_to_end(key)
        return value

    def _purge_expired(self):
        now = self.clock()
        for key, (_, expires_at) in list(self.items.items()):
            if now >= expires_at:
                del self.items[key]
''',
        },
        "tests": '''import pathlib
import sys
import unittest

workspace = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from ttl_cache import TTLCache


class TTLCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.cache = TTLCache(2, clock=lambda: self.now[0])

    def test_returns_live_values_and_missing_default(self):
        self.cache.set("theme", "dark", 10)
        self.assertEqual(self.cache.get("theme"), "dark")
        self.assertEqual(self.cache.get("missing", "light"), "light")

    def test_expired_values_are_not_returned(self):
        self.cache.set("session", "abc", 2)
        self.now[0] = 102.0
        self.assertIsNone(self.cache.get("session"))
        self.assertEqual(self.cache.get("session", "gone"), "gone")

    def test_recently_read_key_survives_lru_eviction(self):
        self.cache.set("a", 1, 50)
        self.cache.set("b", 2, 50)
        self.assertEqual(self.cache.get("a"), 1)
        self.cache.set("c", 3, 50)
        self.assertIsNone(self.cache.get("b"))
        self.assertEqual(self.cache.get("a"), 1)
        self.assertEqual(self.cache.get("c"), 3)

    def test_expired_entries_are_purged_before_live_lru_eviction(self):
        self.cache.set("live", 1, 50)
        self.cache.set("expired", 2, 1)
        self.now[0] = 102.0
        self.cache.set("new", 3, 50)
        self.assertEqual(self.cache.get("live"), 1)
        self.assertIsNone(self.cache.get("expired"))
        self.assertEqual(self.cache.get("new"), 3)

    def test_zero_ttl_does_not_evict_a_live_value(self):
        cache = TTLCache(1, clock=lambda: 100.0)
        cache.set("live", "keep", 5)
        cache.set("expired", "discard", 0)
        self.assertEqual(cache.get("live"), "keep")
        self.assertIsNone(cache.get("expired"))

    def test_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            TTLCache(0, clock=lambda: 0)
        for capacity in (-1, True, 1.5, "2"):
            with self.assertRaises(ValueError):
                TTLCache(capacity, clock=lambda: 0)
        with self.assertRaises(ValueError):
            self.cache.set("bad", "value", -0.1)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
''',
        "write_paths": ["ttl_cache.py"],
    },
    "json-task-store": {
        "category": "feature",
        "goal": "Add a small JSON task store that persists atomically and handles corrupt data without overwriting it.",
        "acceptance": [
            "TaskStore(path).list() returns an empty list for a missing file; add(title) rejects blank or non-string titles with ValueError without creating a file, trims valid titles, and returns the new record.",
            "Persist records as dictionaries with exactly nonempty string id and title fields; in a stored list, each id is task-N for that record's one-based position, and add creates this sequence across TaskStore instances.",
            "Write through a same-directory temporary file and atomically replace the target JSON file with os.replace or Path.replace. If replacement fails, preserve the existing bytes, clean up the temporary file, and raise OSError or TaskStoreError.",
            "Raise TaskStoreError for corrupt JSON, corrupt UTF-8, or structurally invalid stored data, leaving it intact.",
        ],
        "files": {
            "task_store.py": '''import json
from pathlib import Path


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)

    def list(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def add(self, title):
        self.path.write_text(json.dumps([{"title": title}]), encoding="utf-8")
''',
        },
        "solution": {
            "task_store.py": '''import json
import os
from pathlib import Path
import tempfile


class TaskStoreError(RuntimeError):
    pass


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)

    def list(self):
        if not self.path.exists():
            return []
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TaskStoreError("cannot read task store") from error
        if not isinstance(records, list) or any(
            not isinstance(record, dict)
            or set(record) != {"id", "title"}
            or not isinstance(record["id"], str)
            or not isinstance(record["title"], str)
            or not record["id"]
            or not record["title"]
            or record["id"] != "task-%d" % index
            for index, record in enumerate(records, start=1)
        ):
            raise TaskStoreError("task store has invalid structure")
        return records

    def add(self, title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be non-empty")
        records = self.list()
        record = {"id": "task-%d" % (len(records) + 1), "title": title.strip()}
        records.append(record)
        self._write(records)
        return record

    def _write(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(records, handle, sort_keys=True)
                handle.write("\\n")
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise TaskStoreError("cannot write task store") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
''',
        },
        "tests": '''import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

workspace = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from task_store import TaskStore, TaskStoreError


class TaskStoreTests(unittest.TestCase):
    def test_adds_and_persists_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks.json"
            store = TaskStore(path)
            self.assertEqual(store.list(), [])
            self.assertEqual(store.add("  Write release notes  "), {"id": "task-1", "title": "Write release notes"})
            self.assertEqual(TaskStore(path).add("Review changelog"), {"id": "task-2", "title": "Review changelog"})
            self.assertEqual([task["title"] for task in TaskStore(path).list()], ["Write release notes", "Review changelog"])

    def test_nonblank_unicode_titles_roundtrip_without_temporary_files(self):
        for title in ("Unicode " + chr(0x1F680), chr(0xD800)):
            with self.subTest(title=repr(title)), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "tasks.json"
                store = TaskStore(path)
                expected = {"id": "task-1", "title": title}
                self.assertEqual(store.add(title), expected)
                self.assertEqual(TaskStore(path).list(), [expected])
                self.assertEqual(set(path.parent.iterdir()), {path})

    def test_rejects_blank_and_non_string_titles_without_creating_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks.json"
            for title in ("   ", None, 7):
                with self.assertRaises(ValueError):
                    TaskStore(path).add(title)
                self.assertFalse(path.exists())

    def test_corrupt_json_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks.json"
            path.write_text("{not json", encoding="utf-8")
            store = TaskStore(path)
            with self.assertRaises(TaskStoreError):
                store.list()
            with self.assertRaises(TaskStoreError):
                store.add("Do not overwrite")
            self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_corrupt_utf8_is_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks.json"
            path.write_bytes(b"[\\xff]")
            store = TaskStore(path)
            with self.assertRaises(TaskStoreError):
                store.list()
            with self.assertRaises(TaskStoreError):
                store.add("Do not overwrite")
            self.assertEqual(path.read_bytes(), b"[\\xff]")

    def test_rejects_representative_invalid_record_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tasks.json"
            bad_values = [
                {"title": "not a list"},
                [{"id": "", "title": "empty id"}],
                [{"id": 7, "title": "number id"}],
                [{"id": "task-2", "title": "non-sequential id"}],
                [{"id": "task-1", "title": ""}],
                [{"id": "task-1", "title": "ok", "extra": True}],
            ]
            for value in bad_values:
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(TaskStoreError):
                    TaskStore(path).list()

    def test_failed_atomic_replace_preserves_old_file_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = pathlib.Path(directory)
            path = parent / "tasks.json"
            original = [{"id": "task-1", "title": "Existing"}]
            path.write_text(json.dumps(original), encoding="utf-8")
            observed = []

            def fail_replace(source, destination):
                source_path = pathlib.Path(source)
                observed.append((source_path, pathlib.Path(destination), source_path.exists()))
                raise OSError("replace failed")

            with patch("os.replace", side_effect=fail_replace):
                with patch("pathlib.Path.replace", autospec=True, side_effect=fail_replace):
                    spec = importlib.util.spec_from_file_location("atomic_task_store_probe", workspace / "task_store.py")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    try:
                        spec.loader.exec_module(module)
                        with self.assertRaises((module.TaskStoreError, OSError)):
                            module.TaskStore(path).add("New task")
                    finally:
                        sys.modules.pop(spec.name, None)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0].parent, parent)
            self.assertEqual(observed[0][1], path)
            self.assertTrue(observed[0][2])
            self.assertNotEqual(observed[0][0], path)
            self.assertEqual(list(parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
''',
        "write_paths": ["task_store.py"],
    },
    "sales-ledger-refactor": {
        "category": "refactor",
        "goal": "Refactor sales aggregation into a reusable SalesLedger while preserving summarize_sales behavior for existing callers.",
        "acceptance": [
            "Expose SalesLedger with add(row) and summary() methods.",
            "Keep summarize_sales(rows) as a compatible public function with the same result shape and rounding.",
            "Ensure separate ledgers do not share aggregation state.",
            "Preserve the existing KeyError when a row is missing its required region field.",
        ],
        "files": {
            "sales.py": '''def summarize_sales(rows):
    totals = {}
    count = 0
    total = 0.0
    for row in rows:
        region = row["region"]
        amount = float(row["amount"])
        count += 1
        total += amount
        totals[region] = totals.get(region, 0.0) + amount
    return {
        "count": count,
        "total": round(total, 2),
        "by_region": {region: round(amount, 2) for region, amount in totals.items()},
    }
''',
        },
        "solution": {
            "sales.py": '''class SalesLedger:
    def __init__(self):
        self._count = 0
        self._total = 0.0
        self._by_region = {}

    def add(self, row):
        region = row["region"]
        amount = float(row["amount"])
        self._count += 1
        self._total += amount
        self._by_region[region] = self._by_region.get(region, 0.0) + amount

    def summary(self):
        return {
            "count": self._count,
            "total": round(self._total, 2),
            "by_region": {region: round(amount, 2) for region, amount in self._by_region.items()},
        }


def summarize_sales(rows):
    ledger = SalesLedger()
    for row in rows:
        ledger.add(row)
    return ledger.summary()
''',
        },
        "tests": '''import pathlib
import sys
import unittest

workspace = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from sales import SalesLedger, summarize_sales


class SalesLedgerTests(unittest.TestCase):
    def test_existing_function_keeps_aggregation_shape(self):
        rows = [{"region": "west", "amount": "12.25"}, {"region": "east", "amount": 5}, {"region": "west", "amount": "0.25"}]
        self.assertEqual(summarize_sales(rows), {"count": 3, "total": 17.5, "by_region": {"west": 12.5, "east": 5.0}})

    def test_ledger_accumulates_the_same_result(self):
        ledger = SalesLedger()
        ledger.add({"region": "north", "amount": "1.335"})
        ledger.add({"region": "north", "amount": "1.335"})
        self.assertEqual(ledger.summary(), {"count": 2, "total": 2.67, "by_region": {"north": 2.67}})

    def test_ledgers_keep_independent_state(self):
        first = SalesLedger()
        second = SalesLedger()
        first.add({"region": "west", "amount": 3})
        second.add({"region": "east", "amount": 7})
        self.assertEqual(first.summary(), {"count": 1, "total": 3.0, "by_region": {"west": 3.0}})
        self.assertEqual(second.summary(), {"count": 1, "total": 7.0, "by_region": {"east": 7.0}})

    def test_existing_function_accepts_generators(self):
        rows = ({"region": "west", "amount": amount} for amount in (1, 2))
        self.assertEqual(summarize_sales(rows), {"count": 2, "total": 3.0, "by_region": {"west": 3.0}})

    def test_invalid_rows_still_raise_the_existing_key_error(self):
        with self.assertRaises(KeyError):
            SalesLedger().add({"amount": 3})


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
''',
        "write_paths": ["sales.py"],
    },
    "pipeline-call-chain-analysis": {
        "category": "analysis",
        "goal": "Analyze the three-module order pipeline and write a structured report.json with the requested call-chain and behavior facts.",
        "acceptance": [
            "report.json is a JSON object with entrypoint set to pipeline.publish.build_payload and call_chain exactly equal to [pipeline.publish.build_payload, pipeline.ingest.fetch_orders, pipeline.transform.summarize].",
            "report.json has filter_behavior with boolean false_included, zero_included, empty_string_included, and missing_active_included fields that describe whether each active value reaches the summary.",
            'report.json has summary_fields as a two-item list containing exactly order_count and revenue_cents, plus example_input exactly equal to [{"id": "paid", "cents": "125", "active": true}, {"id": "cancelled", "cents": "900", "active": false}, {"id": "legacy", "cents": "75"}] and example_output equal to the actual build_payload(example_input) result.',
        ],
        "files": {
            "pipeline/__init__.py": '''"""Order pipeline package."""
''',
            "pipeline/ingest.py": '''def fetch_orders(source):
    return [
        {"id": raw["id"], "cents": int(raw["cents"])}
        for raw in source
        if raw.get("active", True)
    ]
''',
            "pipeline/transform.py": '''def summarize(orders):
    return {
        "order_count": len(orders),
        "revenue_cents": sum(order["cents"] for order in orders),
    }
''',
            "pipeline/publish.py": '''from .ingest import fetch_orders
from .transform import summarize


def build_payload(source):
    return {"metrics": summarize(fetch_orders(source))}
''',
        },
        "solution": {
            "report.json": '''{
  "entrypoint": "pipeline.publish.build_payload",
  "call_chain": [
    "pipeline.publish.build_payload",
    "pipeline.ingest.fetch_orders",
    "pipeline.transform.summarize"
  ],
  "filter_behavior": {
    "false_included": false,
    "zero_included": false,
    "empty_string_included": false,
    "missing_active_included": true
  },
  "summary_fields": [
    "order_count",
    "revenue_cents"
  ],
  "example_input": [
    {"id": "paid", "cents": "125", "active": true},
    {"id": "cancelled", "cents": "900", "active": false},
    {"id": "legacy", "cents": "75"}
  ],
  "example_output": {
    "metrics": {
      "order_count": 2,
      "revenue_cents": 200
    }
  }
}
''',
        },
        "tests": '''import importlib.util
import json
import pathlib
import sys
import unittest

workspace = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(workspace))

from pipeline.publish import build_payload


class PipelineAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.report_path = workspace / "report.json"
        self.source = [
            {"id": "paid", "cents": "125", "active": True},
            {"id": "cancelled", "cents": "900", "active": False},
            {"id": "legacy", "cents": "75"},
        ]

    def _report(self):
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def test_pipeline_behavior_used_by_the_analysis(self):
        self.assertEqual(build_payload(self.source), {"metrics": {"order_count": 2, "revenue_cents": 200}})

    def test_report_names_entrypoint_and_call_chain(self):
        report = self._report()
        self.assertEqual(report["entrypoint"], "pipeline.publish.build_payload")
        self.assertEqual(report["call_chain"], ["pipeline.publish.build_payload", "pipeline.ingest.fetch_orders", "pipeline.transform.summarize"])

    def test_report_describes_the_filter_rule(self):
        behavior = self._report()["filter_behavior"]
        expected = {"false_included": False, "zero_included": False, "empty_string_included": False, "missing_active_included": True}
        for key, value in expected.items():
            self.assertIn(key, behavior)
            self.assertIs(type(behavior[key]), bool)
            self.assertEqual(behavior[key], value)
        falsey_source = [
            {"id": "false", "cents": "20", "active": False},
            {"id": "zero", "cents": "30", "active": 0},
            {"id": "empty", "cents": "40", "active": ""},
            {"id": "missing", "cents": "10"},
        ]
        self.assertEqual(build_payload(falsey_source), {"metrics": {"order_count": 1, "revenue_cents": 10}})

    def test_report_records_summary_contract_and_example(self):
        report = self._report()
        self.assertIsInstance(report["summary_fields"], list)
        self.assertEqual(len(report["summary_fields"]), 2)
        self.assertEqual(set(report["summary_fields"]), {"order_count", "revenue_cents"})
        self.assertEqual(report["example_input"], self.source)
        self.assertIs(type(report["example_input"][0]["active"]), bool)
        self.assertIs(type(report["example_input"][1]["active"]), bool)
        self.assertEqual(report["example_output"], build_payload(report["example_input"]))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
''',
        "write_paths": ["report.json"],
    },
}
