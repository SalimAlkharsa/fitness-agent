"""In-memory fake of the slice of the gspread API that SheetsStore uses.

Lets us unit-test the store's logic (headers, roundtrips, upsert, delete, id
allocation, idempotent bootstrap) with no network or credentials. The real
integration test (test_store_integration) exercises the same methods against a
live scratch spreadsheet when creds are present.
"""

from __future__ import annotations

import re

_A1 = re.compile(r"^([A-Z]+)(\d+)$")


def _col_to_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1  # 0-based


class FakeWorksheet:
    def __init__(self, title: str) -> None:
        self.title = title
        self._grid: list[list[str]] = []  # rows of cells (strings)

    # -- helpers --
    def _ensure(self, rows: int, cols: int) -> None:
        while len(self._grid) < rows:
            self._grid.append([])
        for row in self._grid:
            while len(row) < cols:
                row.append("")

    @staticmethod
    def _trim(values: list[str]) -> list[str]:
        out = list(values)
        while out and out[-1] == "":
            out.pop()
        return out

    # -- reads --
    def row_values(self, r: int) -> list[str]:
        if r > len(self._grid):
            return []
        return self._trim(list(self._grid[r - 1]))

    def col_values(self, c: int) -> list[str]:
        idx = c - 1
        col = [row[idx] if idx < len(row) else "" for row in self._grid]
        return self._trim(col)

    def get_all_records(self, expected_headers: list[str] | None = None) -> list[dict]:
        if not self._grid:
            return []
        headers = self._trim(list(self._grid[0]))
        records = []
        for row in self._grid[1:]:
            rec = {}
            for i, h in enumerate(headers):
                rec[h] = row[i] if i < len(row) else ""
            records.append(rec)
        return records

    def get_all_values(self) -> list[list[str]]:
        # Rectangular copy up to the last non-empty row (gspread semantics).
        last = 0
        for i, r in enumerate(self._grid, start=1):
            if self._trim(r):
                last = i
        rows = [list(r) for r in self._grid[:last]]
        width = max((len(r) for r in rows), default=0)
        return [r + [""] * (width - len(r)) for r in rows]

    def clear(self) -> None:
        self._grid = []

    # -- writes --
    def update(self, values: list[list[str]], a1: str) -> None:
        m = _A1.match(a1)
        assert m, f"unsupported range {a1!r}"
        start_col = _col_to_idx(m.group(1))
        start_row = int(m.group(2)) - 1
        height = len(values)
        width = max(len(r) for r in values)
        self._ensure(start_row + height, start_col + width)
        for dr, rowvals in enumerate(values):
            for dc, val in enumerate(rowvals):
                self._grid[start_row + dr][start_col + dc] = _stringify(val)

    def append_row(self, row: list, value_input_option: str = "RAW") -> None:
        self.append_rows([row], value_input_option)

    def append_rows(self, rows: list[list], value_input_option: str = "RAW") -> None:
        # Append after the last non-empty row (gspread semantics).
        last = 0
        for i, r in enumerate(self._grid, start=1):
            if self._trim(r):
                last = i
        for offset, row in enumerate(rows):
            target = last + offset
            self._ensure(target + 1, len(row))
            self._grid[target] = [_stringify(v) for v in row] + [""] * (
                len(self._grid[target]) - len(row)
            )

    def delete_rows(self, idx: int) -> None:
        if 1 <= idx <= len(self._grid):
            self._grid.pop(idx - 1)


def _stringify(v: object) -> str:
    if v is None:
        return ""
    return str(v)


class FakeClient:
    """Stands in for gspread.Client — records Drive copy() calls (snapshots)."""

    def __init__(self) -> None:
        self.copies: list[str] = []

    def copy(self, file_id: str, title: str | None = None, **kwargs):
        self.copies.append(title or file_id)
        snap = FakeSpreadsheet(title=title or "copy")
        return snap


class FakeSpreadsheet:
    def __init__(self, title: str = "scratch", id: str = "fake-sheet-id") -> None:
        self.title = title
        self.id = id
        self.client = FakeClient()
        # New Google spreadsheets ship with a default "Sheet1".
        self._sheets: list[FakeWorksheet] = [FakeWorksheet("Sheet1")]

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self._sheets)

    def worksheet(self, name: str) -> FakeWorksheet:
        for ws in self._sheets:
            if ws.title == name:
                return ws
        raise KeyError(name)

    def add_worksheet(self, title: str, rows: int = 100, cols: int = 20) -> FakeWorksheet:
        ws = FakeWorksheet(title)
        self._sheets.append(ws)
        return ws

    def del_worksheet(self, ws: FakeWorksheet) -> None:
        self._sheets = [s for s in self._sheets if s.title != ws.title]
