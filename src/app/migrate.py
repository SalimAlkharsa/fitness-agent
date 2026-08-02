"""Migration runner + startup schema-version gate.

Google Sheets has no ALTER TABLE, so schema changes are versioned scripts under
`migrations/NNN_name.py`, each exposing `apply(store)`. The runner:

  1. reads the sheet's current `_meta.schema_version`,
  2. finds pending migrations (number > current),
  3. for each, in order: snapshots the whole spreadsheet via Drive, runs
     `apply(store)`, then bumps `_meta.schema_version` to that migration's number.

`python -m app.migrate` applies pending migrations; `--status` just reports.

The web layer (app.web.get_store) calls `check_schema_version`, which refuses
to boot on a code/sheet version mismatch — a deploy can never write new-shape
rows into old-shape tabs.
"""

from __future__ import annotations

import datetime
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from app import SCHEMA_VERSION
from app.store import SheetsStore

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"
_NAME_RE = re.compile(r"^(\d+)_.+\.py$")


class SchemaVersionError(RuntimeError):
    """Raised when the code and sheet schema versions disagree."""


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    path: Path

    def load(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(f"migration_{self.number}", self.path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load migration {self.path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "apply"):
            raise RuntimeError(f"migration {self.name} has no apply(store) function")
        return module


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All migrations in a dir, sorted by number. Non-matching files are ignored."""
    out: list[Migration] = []
    if not migrations_dir.is_dir():
        return out
    for path in sorted(migrations_dir.iterdir()):
        m = _NAME_RE.match(path.name)
        if m:
            out.append(Migration(number=int(m.group(1)), name=path.name, path=path))
    out.sort(key=lambda mig: mig.number)
    return out


def check_schema_version(store: SheetsStore, code_version: int = SCHEMA_VERSION) -> None:
    """Raise SchemaVersionError with actionable text on a version mismatch."""
    sheet_version = store.schema_version
    if sheet_version != code_version:
        raise SchemaVersionError(
            f"Schema version mismatch: code expects {code_version}, sheet is at "
            f"{sheet_version}. Run `python -m app.migrate` to apply pending "
            f"migrations (or check out the code version matching the sheet)."
        )


def run(
    store: SheetsStore,
    today: str,
    migrations_dir: Path = MIGRATIONS_DIR,
    do_snapshot: bool = True,
) -> list[int]:
    """Apply pending migrations in order. Returns the list of numbers applied.

    `today` (YYYY-MM-DD) is injected rather than read from the clock so tests are
    deterministic.
    """
    current = store.schema_version
    pending = [m for m in discover(migrations_dir) if m.number > current]
    applied: list[int] = []
    for mig in pending:
        if do_snapshot:
            label = f"{today} pre-migration-{mig.number:03d}"
            snap = store.snapshot(label)
            print(f"  snapshot: {snap}")
        module = mig.load()
        print(f"  applying {mig.name} …")
        module.apply(store)
        store.schema_version = mig.number
        applied.append(mig.number)
    return applied


def status(store: SheetsStore, migrations_dir: Path = MIGRATIONS_DIR) -> tuple[int, int]:
    current = store.schema_version
    migs = discover(migrations_dir)
    latest = max((m.number for m in migs), default=current)
    return current, latest


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    store = SheetsStore.open()
    if "--status" in argv:
        current, latest = status(store)
        pending = [m.name for m in discover() if m.number > current]
        print(f"Sheet schema version: {current}")
        print(f"Latest migration:     {latest}")
        print(f"Code SCHEMA_VERSION:  {SCHEMA_VERSION}")
        print("Pending: " + (", ".join(pending) if pending else "none — up to date"))
        return

    today = datetime.date.today().isoformat()
    applied = run(store, today)
    if applied:
        print(f"Applied migrations: {applied}. Sheet now at version {store.schema_version}.")
    else:
        print("No pending migrations — already up to date.")


if __name__ == "__main__":
    main()
