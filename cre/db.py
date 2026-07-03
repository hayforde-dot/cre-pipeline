from __future__ import annotations
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).parent / "schema.sql"


def connect(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA.read_text())
    return con


def get_assumption(con, deal_id: int, key: str, default: float | None = None) -> float:
    row = con.execute(
        "SELECT value FROM deal_assumptions WHERE deal_id=? AND key=?", (deal_id, key)
    ).fetchone()
    if row is None:
        if default is None:
            raise KeyError(f"assumption '{key}' missing for deal {deal_id} and no default")
        return default
    return row["value"]


def list_placeholders(con, deal_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT key, value, note FROM deal_assumptions WHERE deal_id=? AND is_placeholder=1 ORDER BY key",
        (deal_id,),
    ).fetchall()
    return [dict(r) for r in rows]
