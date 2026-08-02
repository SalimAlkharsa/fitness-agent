# Migrations

Google Sheets has no `ALTER TABLE`, so every schema change is a versioned script
here. The runner (`python -m app.migrate`) snapshots the whole spreadsheet to
Drive, applies each pending script in order, and bumps `_meta.schema_version`.

## Writing a migration

Create `migrations/NNN_short_name.py` where `NNN` is the next integer (zero-padded,
e.g. `002_add_rpe.py`). Bump `SCHEMA_VERSION` in `src/app/__init__.py` to `NNN` in
the **same commit**, and update the model(s) in `src/app/models.py` and the tab
headers in `src/app/store.py` to match.

Each script exposes one function:

```python
def apply(store):
    ...
```

`store` is an `app.store.SheetsStore`. Reach the raw worksheet with
`store._ws("sets")` when you need column-level changes.

## Standard pattern: full-tab rewrite

Data is tiny (one user), so the safe, simple pattern is read → transform → clear →
write back:

```python
def apply(store):
    ws = store._ws("sets")
    values = ws.get_all_values()      # includes the header row
    if not values:
        return
    header = values[0]
    if "rpe" in header:               # idempotent guard
        return
    header.append("rpe")
    rows = [header] + [row + [""] for row in values[1:]]   # backfill empty
    ws.clear()
    ws.update(rows, "A1")
```

## Safety

- The runner copies the entire spreadsheet to Drive first, named
  `YYYY-MM-DD pre-migration-NNN`. Restore by copying that sheet back over the live
  one (see the deploy runbook).
- App startup asserts `SCHEMA_VERSION` (code) == `_meta.schema_version` (sheet)
  and refuses to boot on a mismatch, so a new deploy can never write new-shape
  rows into old-shape tabs before the migration runs.
- Nuclear option: replay every `raw_text` through the parser into fresh tabs.
