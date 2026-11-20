"""Relations between ECU files, software revisions and references, with evidence.

A relation links two hashed subjects (``file:<sha256>``, ``variant:<id>``,
``family:<key>``, ``sw:<normalized>``) with a *kind* and a *status*:

kinds
    ``exact_content``          identical SHA-256
    ``same_family``            same ECU family (not software compatibility!)
    ``related_revision``       other revision of the same software base
    ``verified_compatibility`` the specific file/software is validated for a profile
    ``conflict``               contradicting identification

statuses
    ``proposed``               measured or suggested, not confirmed
    ``user_confirmed``         a reviewer agreed - *not* independent evidence
    ``validated``              backed by a reproducible independent check
                               (see :data:`INDEPENDENT_EVIDENCE`)
    ``rejected``               reviewed and refused
    ``conflicting``            sources disagree
    ``stale``                  the source file's hash changed since

Rules
* A review never turns a relation into ``validated`` by itself: that needs at
  least one evidence item of an independent kind.
* Only ``validated`` relations are reused automatically (:meth:`reusable`).
* Nothing is deleted: superseded and refused relations stay traceable, every
  status change is a row in ``evidence_reviews``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GRAPH_VERSION = "evidence-graph/1"
DB_FILE = "evidence.sqlite"
KINDS = ("exact_content", "same_family", "related_revision", "verified_compatibility", "conflict")
STATUSES = ("proposed", "user_confirmed", "validated", "rejected", "conflicting", "stale")
DECISIONS = {"confirm": "user_confirmed", "reject": "rejected", "conflict": "conflicting", "reopen": "proposed"}
# Evidence kinds that are reproducible checks independent of the reviewer.
INDEPENDENT_EVIDENCE = {
    "sha256_identical",
    "winols_export_sha256_match",
    "practice_reproduced_real_tune",
    "transfer_dry_run_identical_bytes",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    object TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT NOT NULL,           -- JSON list of {kind, detail, source_sha256}
    source_sha256 TEXT NOT NULL,      -- exact hash of the file the relation was measured on
    algorithm TEXT NOT NULL,
    original_values TEXT NOT NULL,    -- JSON: identification fields as found, unnormalized
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS evidence_relations_subject ON evidence_relations(subject);
CREATE INDEX IF NOT EXISTS evidence_relations_object ON evidence_relations(object);
CREATE TABLE IF NOT EXISTS evidence_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL REFERENCES evidence_relations(id),
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    source_hashes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceGraph:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(root / DB_FILE))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO evidence_meta(key,value) VALUES('schema',?)", (GRAPH_VERSION,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ write
    def propose(self, kind: str, subject: str, obj: str, *, evidence: list[dict[str, Any]],
                source_sha256: str, algorithm: str, original_values: dict[str, Any] | None = None,
                created_by: str = "engine") -> int:
        if kind not in KINDS:
            raise ValueError(f"Onbekende relatiesoort: {kind}")
        if not source_sha256:
            raise ValueError("Een relatie heeft altijd de exacte bronhash nodig.")
        status = "validated" if _independent(evidence) else "proposed"
        cur = self.db.execute(
            "INSERT INTO evidence_relations(kind,subject,object,status,evidence,source_sha256,algorithm,"
            "original_values,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, subject, obj, status, json.dumps(evidence, sort_keys=True), source_sha256, algorithm,
             json.dumps(original_values or {}, sort_keys=True), _now(), created_by))
        self.db.commit()
        return int(cur.lastrowid)

    def review(self, relation_id: int, decision: str, reason: str, reviewer: str,
               current_source_sha256: str | None = None) -> dict[str, Any]:
        """Record a reviewer decision. ``confirm`` gives ``user_confirmed``,
        never ``validated``; a changed source hash makes the relation stale."""

        if decision not in DECISIONS:
            raise ValueError(f"Beslissing moet een van {sorted(DECISIONS)} zijn.")
        if not reason.strip() or not reviewer.strip():
            raise ValueError("Reden en beoordelaar zijn verplicht.")
        row = self.get(relation_id)
        if row is None:
            raise KeyError(relation_id)
        previous = row["status"]
        if current_source_sha256 and current_source_sha256 != row["source_sha256"]:
            new_status = "stale"
        elif decision == "confirm" and previous == "validated":
            new_status = "validated"  # a confirmation does not weaken independent evidence
        else:
            new_status = DECISIONS[decision]
        self.db.execute("UPDATE evidence_relations SET status=? WHERE id=?", (new_status, relation_id))
        self.db.execute(
            "INSERT INTO evidence_reviews(relation_id,decision,reason,reviewer,reviewed_at,previous_status,"
            "new_status,source_hashes) VALUES(?,?,?,?,?,?,?,?)",
            (relation_id, decision, reason, reviewer, _now(), previous, new_status,
             json.dumps(sorted({row["source_sha256"], current_source_sha256 or row["source_sha256"]}))))
        self.db.commit()
        return self.get(relation_id)

    def add_evidence(self, relation_id: int, item: dict[str, Any], reviewer: str = "engine") -> dict[str, Any]:
        """Attach a new reproducible check; promotes to ``validated`` only for
        independent evidence kinds measured on the same source hash."""

        row = self.get(relation_id)
        if row is None:
            raise KeyError(relation_id)
        evidence = row["evidence"] + [item]
        previous = row["status"]
        new_status = previous
        if (previous in ("proposed", "user_confirmed") and item.get("kind") in INDEPENDENT_EVIDENCE
                and item.get("source_sha256") == row["source_sha256"]):
            new_status = "validated"
        self.db.execute("UPDATE evidence_relations SET evidence=?,status=? WHERE id=?",
                        (json.dumps(evidence, sort_keys=True), new_status, relation_id))
        if new_status != previous:
            self.db.execute(
                "INSERT INTO evidence_reviews(relation_id,decision,reason,reviewer,reviewed_at,previous_status,"
                "new_status,source_hashes) VALUES(?,?,?,?,?,?,?,?)",
                (relation_id, "evidence", f"{item.get('kind')}: {item.get('detail', '')}", reviewer, _now(),
                 previous, new_status, json.dumps([row["source_sha256"]])))
        self.db.commit()
        return self.get(relation_id)

    def mark_stale(self, subject: str, current_sha256: str) -> int:
        """Mark relations measured on an older version of ``subject`` as stale."""

        rows = self.db.execute("SELECT id,status FROM evidence_relations WHERE subject=? AND source_sha256!=? "
                               "AND status!='stale'", (subject, current_sha256)).fetchall()
        for row in rows:
            self.db.execute("UPDATE evidence_relations SET status='stale' WHERE id=?", (row["id"],))
            self.db.execute(
                "INSERT INTO evidence_reviews(relation_id,decision,reason,reviewer,reviewed_at,previous_status,"
                "new_status,source_hashes) VALUES(?,?,?,?,?,?,?,?)",
                (row["id"], "stale", "bronbestand gewijzigd", "engine", _now(), row["status"], "stale",
                 json.dumps([current_sha256])))
        self.db.commit()
        return len(rows)

    # ------------------------------------------------------------------ read
    def get(self, relation_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM evidence_relations WHERE id=?", (relation_id,)).fetchone()
        return _row(row) if row else None

    def relations(self, subject: str | None = None, obj: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM evidence_relations WHERE 1=1", []
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        if obj:
            sql += " AND object=?"
            args.append(obj)
        return [_row(r) for r in self.db.execute(sql + " ORDER BY id", args)]

    def between(self, a: str, b: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM evidence_relations WHERE (subject=? AND object=?) OR "
                               "(subject=? AND object=?) ORDER BY id", (a, b, b, a))
        return [_row(r) for r in rows]

    def reusable(self, a: str, b: str) -> list[dict[str, Any]]:
        """Relations the engine may reuse automatically: validated only."""

        return [r for r in self.between(a, b) if r["status"] == "validated"]

    def history(self, relation_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM evidence_reviews WHERE relation_id=? ORDER BY id", (relation_id,))]


def _independent(evidence: list[dict[str, Any]]) -> bool:
    return any(item.get("kind") in INDEPENDENT_EVIDENCE for item in evidence)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["evidence"] = json.loads(out["evidence"])
    out["original_values"] = json.loads(out["original_values"])
    return out
