"""`python -m app.bootstrap` — create the five tabs, seed _meta and the starter KB.

Idempotent: running twice changes nothing. Requires GOOGLE_SA_JSON and SHEET_ID.
"""

from __future__ import annotations

from app.kb_seed import STARTER_KB
from app.store import SheetsStore


def main() -> None:
    store = SheetsStore.open()
    store.bootstrap(kb_seed=STARTER_KB)
    tabs = ", ".join(ws.title for ws in store.spreadsheet.worksheets())
    print(f"Bootstrap complete. schema_version={store.schema_version}. Tabs: {tabs}")
    print(f"KB entries: {len(store.kb_entries())}")


if __name__ == "__main__":
    main()
