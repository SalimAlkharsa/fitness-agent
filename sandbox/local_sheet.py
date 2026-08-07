"""The mock Google Sheet: tabs kept in one JSON file you can open and read.

Reuses the in-memory gspread fake the unit suite already trusts
(`tests/fake_sheets`) and adds load/save, so a sandbox run leaves an inspectable
artifact instead of vanishing. The fiddly part — A1 updates, append-after-last-
non-empty-row, header semantics — is exactly what that fake encodes, so it is
reused rather than reimplemented here.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fake_sheets import FakeSpreadsheet, FakeWorksheet

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "sheet.json"


class LocalSheet(FakeSpreadsheet):
    """A `gspread.Spreadsheet` stand-in persisted to `path` as {tab: rows}."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        super().__init__(title="local-sandbox", id="local-sheet")
        self.path = Path(path)
        if self.path.is_file():
            self._sheets = []
            for title, grid in json.loads(self.path.read_text()).items():
                self._sheets.append(FakeWorksheet(title))
                self._sheets[-1]._grid = [list(row) for row in grid]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({w.title: w._grid for w in self._sheets}, indent=1) + "\n")
