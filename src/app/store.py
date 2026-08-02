"""Google Sheets datastore layer.

Wraps gspread with typed methods so nothing else in the codebase touches raw
cells: every value crossing this boundary is a Pydantic model, never a
dict-of-strings.

Five tabs (see DESIGN.md): workouts, sets, exercise_kb, inbox, _meta.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import gspread
from google.oauth2.service_account import Credentials

from app import SCHEMA_VERSION
from app.config import settings
from app.models import Equipment, InboxItem, KBEntry, SetGroup, Workout

if TYPE_CHECKING:
    from gspread import Spreadsheet, Worksheet

# Read+write on Sheets, plus Drive scope so the migration snapshotter can copy.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Header rows — the column order is the contract with every read/write below.
WORKOUTS_HEADERS = [
    "workout_id",
    "date",
    "type",
    "start",
    "end",
    "notes",
    "raw_text",
    "logged_at",
]
SETS_HEADERS = [
    "workout_id",
    "exercise",
    "exercise_raw",
    "grp",
    "sets",
    "reps",
    "per_side_lbs",
    "total_lbs",
    "assist_lbs",
    "notes",
]
KB_HEADERS = ["canonical", "aliases", "equipment", "base_lbs", "notes"]
INBOX_HEADERS = ["timestamp", "raw_text", "error"]
META_HEADERS = ["key", "value"]

TABS: dict[str, list[str]] = {
    "workouts": WORKOUTS_HEADERS,
    "sets": SETS_HEADERS,
    "exercise_kb": KB_HEADERS,
    "inbox": INBOX_HEADERS,
    "_meta": META_HEADERS,
}

ALIAS_SEP = ";"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Sheets enforces per-minute quotas and a bootstrap or undo can burst past them,
# so back off and retry rather than surfacing a transient 429 to callers.
_RETRYABLE = frozenset({429, 500, 502, 503})


# TypeVar, not PEP-695 `def _retry[T]` — the deployed runtime is Python 3.11.
T = TypeVar("T")


def _retry(fn: Callable[[], T], tries: int = 6, base: float = 2.0) -> T:
    for i in range(tries):
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in _RETRYABLE and i < tries - 1:
                time.sleep(base * (2**i))  # 2,4,8,16,32s — clears a 1-min quota window
                continue
            raise


def _s(value: object) -> str:
    """Render a cell value for the sheet: None → empty string, 3.0 → "3"."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _opt_str(cell: str) -> str | None:
    cell = (cell or "").strip()
    return cell or None


def _opt_float(cell: str) -> float | None:
    cell = (cell or "").strip()
    return float(cell) if cell else None


class SheetsStore:
    """Typed wrapper over one spreadsheet. Construct with `SheetsStore.open()`."""

    def __init__(self, spreadsheet: Spreadsheet) -> None:
        self._ss = spreadsheet

    # --- construction ---

    @classmethod
    def open(cls, sa_json: str | None = None, sheet_id: str | None = None) -> SheetsStore:
        sa_json = sa_json or settings.google_sa_json
        sheet_id = sheet_id or settings.sheet_id
        if not sa_json:
            raise RuntimeError("GOOGLE_SA_JSON is not set — cannot open the sheet.")
        if not sheet_id:
            raise RuntimeError("SHEET_ID is not set — cannot open the sheet.")
        # GOOGLE_SA_JSON is a file path locally, or the JSON itself on serverless —
        # DO Functions has no filesystem secrets, so the key rides in an env var.
        if sa_json.lstrip().startswith("{"):
            creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(sa_json, scopes=SCOPES)
        client = gspread.authorize(creds)
        return cls(client.open_by_key(sheet_id))

    @property
    def spreadsheet(self) -> Spreadsheet:
        return self._ss

    def _ws(self, name: str) -> Worksheet:
        return self._ss.worksheet(name)

    def _records(self, name: str) -> list[dict]:
        """Read a whole tab as dict rows, with quota backoff."""
        return _retry(lambda: self._ws(name).get_all_records(expected_headers=TABS[name]))

    def snapshot(self, label: str) -> str:
        """Copy the entire spreadsheet via the Drive API (pre-migration backup).

        Returns the new spreadsheet's title.
        """
        copy = _retry(lambda: self._ss.client.copy(self._ss.id, title=label))
        return getattr(copy, "title", label)

    # --- bootstrap ---

    def bootstrap(self, kb_seed: list[KBEntry] | None = None) -> None:
        """Create the five tabs with headers, seed _meta and the starter KB.

        Idempotent: existing tabs keep their data; headers are (re)written;
        _meta and KB seeds are only written when missing. Reads/writes are
        batched to stay inside the Sheets per-minute quota.
        """
        existing = {ws.title for ws in _retry(self._ss.worksheets)}
        for name, headers in TABS.items():
            if name not in existing:
                _retry(
                    lambda name=name, headers=headers: self._ss.add_worksheet(
                        title=name, rows=100, cols=max(len(headers), 5)
                    )
                )
            ws = self._ws(name)
            current = _retry(lambda ws=ws: ws.row_values(1))
            if current != headers:
                _retry(lambda ws=ws, headers=headers: ws.update([headers], "A1"))

        meta = self._meta_dict()
        if "schema_version" not in meta:
            self._meta_set("schema_version", SCHEMA_VERSION)
        if "workout_id_counter" not in meta:
            self._meta_set("workout_id_counter", 0)

        # One append for all missing KB entries, not one write per row.
        if kb_seed:
            have = {e.canonical for e in self.kb_entries()}
            new_rows = [self._kb_row(e) for e in kb_seed if e.canonical not in have]
            if new_rows:
                _retry(
                    lambda: self._ws("exercise_kb").append_rows(new_rows, value_input_option="RAW")
                )

        # gspread leaves an empty default "Sheet1" on brand-new spreadsheets.
        titles = {ws.title for ws in _retry(self._ss.worksheets)}
        if "Sheet1" in titles and "Sheet1" not in TABS:
            try:
                _retry(lambda: self._ss.del_worksheet(self._ws("Sheet1")))
            except gspread.exceptions.GSpreadException:
                pass

    # --- workouts ---

    def append_workout(self, workout: Workout) -> None:
        row = [
            workout.workout_id,
            workout.date,
            workout.type,
            _s(workout.start),
            _s(workout.end),
            _s(workout.notes),
            workout.raw_text,
            workout.logged_at,
        ]
        _retry(lambda: self._ws("workouts").append_row(row, value_input_option="RAW"))

    def read_workouts(self) -> list[Workout]:
        records = self._records("workouts")
        return [
            Workout(
                workout_id=str(r["workout_id"]),
                date=str(r["date"]),
                type=str(r["type"]),
                start=_opt_str(str(r["start"])),
                end=_opt_str(str(r["end"])),
                notes=_opt_str(str(r["notes"])),
                raw_text=str(r["raw_text"]),
                logged_at=str(r["logged_at"]),
            )
            for r in records
            if str(r["workout_id"]).strip()
        ]

    # --- sets ---

    def append_sets(self, sets: list[SetGroup]) -> None:
        if not sets:
            return
        rows = [
            [
                sg.workout_id,
                sg.exercise,
                sg.exercise_raw,
                sg.grp,
                sg.sets,
                sg.reps,
                _s(sg.per_side_lbs),
                _s(sg.total_lbs),
                _s(sg.assist_lbs),
                _s(sg.notes),
            ]
            for sg in sets
        ]
        _retry(lambda: self._ws("sets").append_rows(rows, value_input_option="RAW"))

    def read_sets(
        self,
        exercise: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[SetGroup]:
        records = self._records("sets")
        out: list[SetGroup] = []
        # Date filtering needs the parent workout's date.
        wdate = (
            {w.workout_id: w.date for w in self.read_workouts()} if (date_from or date_to) else {}
        )
        for r in records:
            wid = str(r["workout_id"]).strip()
            if not wid:
                continue
            sg = SetGroup(
                workout_id=wid,
                exercise=str(r["exercise"]),
                exercise_raw=str(r["exercise_raw"]),
                grp=int(r["grp"]),
                sets=int(r["sets"]),
                reps=float(r["reps"]),
                per_side_lbs=_opt_float(str(r["per_side_lbs"])),
                total_lbs=_opt_float(str(r["total_lbs"])),
                assist_lbs=_opt_float(str(r["assist_lbs"])),
                notes=_opt_str(str(r["notes"])),
            )
            if exercise and sg.exercise.lower() != exercise.lower():
                continue
            if date_from or date_to:
                d = wdate.get(wid, "")
                # Only ISO YYYY-MM-DD dates are range-comparable; anything else
                # (e.g. the "unknown" date backfill assigns) is dropped from a
                # bounded query rather than compared lexicographically by accident.
                if not _ISO_DATE.match(d):
                    continue
                if date_from and d < date_from:
                    continue
                if date_to and d > date_to:
                    continue
            out.append(sg)
        return out

    # --- exercise KB ---

    @staticmethod
    def _kb_row(entry: KBEntry) -> list[str]:
        return [
            entry.canonical,
            ALIAS_SEP.join(entry.aliases),
            str(entry.equipment),
            _s(entry.base_lbs),
            _s(entry.notes),
        ]

    def kb_entries(self) -> list[KBEntry]:
        records = self._records("exercise_kb")
        out: list[KBEntry] = []
        for r in records:
            canonical = str(r["canonical"]).strip()
            if not canonical:
                continue
            aliases_raw = str(r["aliases"]).strip()
            aliases = [a.strip() for a in aliases_raw.split(ALIAS_SEP) if a.strip()]
            out.append(
                KBEntry(
                    canonical=canonical,
                    aliases=aliases,
                    equipment=Equipment(str(r["equipment"]).strip()),
                    base_lbs=float(r["base_lbs"] or 0),
                    notes=_opt_str(str(r["notes"])),
                )
            )
        return out

    def upsert_kb(self, entry: KBEntry) -> None:
        """Insert or replace the KB row keyed by canonical name."""
        ws = self._ws("exercise_kb")
        row = self._kb_row(entry)
        col = _retry(lambda: ws.col_values(1))  # canonical column, including header
        for idx, existing in enumerate(col[1:], start=2):
            if existing.strip() == entry.canonical:
                _retry(lambda idx=idx: ws.update([row], f"A{idx}"))
                return
        _retry(lambda: ws.append_row(row, value_input_option="RAW"))

    # --- inbox ---

    def to_inbox(self, item: InboxItem) -> None:
        _retry(
            lambda: self._ws("inbox").append_row(
                [item.timestamp, item.raw_text, item.error], value_input_option="RAW"
            )
        )

    # --- delete / undo ---

    def delete_workout(self, workout_id: str) -> int:
        """Remove the workout row and all its sets rows. Returns rows deleted."""
        deleted = 0
        wws = self._ws("workouts")
        wcol = _retry(lambda: wws.col_values(1))
        for idx in range(len(wcol), 1, -1):  # bottom-up so indices stay valid
            if wcol[idx - 1].strip() == workout_id:
                _retry(lambda idx=idx: wws.delete_rows(idx))
                deleted += 1
        sws = self._ws("sets")
        scol = _retry(lambda: sws.col_values(1))
        for idx in range(len(scol), 1, -1):
            if scol[idx - 1].strip() == workout_id:
                _retry(lambda idx=idx: sws.delete_rows(idx))
                deleted += 1
        return deleted

    # --- _meta ---

    def _meta_dict(self) -> dict[str, str]:
        records = self._records("_meta")
        return {str(r["key"]): str(r["value"]) for r in records if str(r["key"]).strip()}

    def _meta_set(self, key: str, value: object) -> None:
        ws = self._ws("_meta")
        col = _retry(lambda: ws.col_values(1))
        for idx, existing in enumerate(col[1:], start=2):
            if existing.strip() == key:
                _retry(lambda idx=idx: ws.update([[key, _s(value)]], f"A{idx}"))
                return
        _retry(lambda: ws.append_row([key, _s(value)], value_input_option="RAW"))

    @property
    def schema_version(self) -> int:
        return int(self._meta_dict().get("schema_version", 0))

    @schema_version.setter
    def schema_version(self, value: int) -> None:
        self._meta_set("schema_version", value)

    def next_workout_id(self) -> str:
        """Increment and return the next id, formatted W0001, W0002, …

        Best-effort atomicity is fine (single user, per DESIGN.md)."""
        counter = int(self._meta_dict().get("workout_id_counter", 0)) + 1
        self._meta_set("workout_id_counter", counter)
        return f"W{counter:04d}"
