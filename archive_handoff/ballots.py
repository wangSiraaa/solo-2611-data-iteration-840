"""Ballot reconciliation and exact fractional transfer counting.

This module is a *separate* offline aid for the fictional club election.  It
does not touch custody or preservation rules.  It only:

* normalises the two clerks' transcriptions of one stable original ballot;
* adopts a ballot when both drafts normalise to the same sequence (even when
  the drafts were filed under different case numbers), or records a traceable
  librarian ruling that supplies the effective sequence or spoils the ballot;
* keeps a revision history of rulings so an older ruling can still be read;
* exports the effective (or as-transcribed) ballot sequences as a standalone
  JSON document; and
* counts seats with the single-transferable-vote procedure, reporting exact
  fractions per round (totals, retained, transferred, exhausted, elected).

Nothing here performs a real deletion; all names are fictional.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from fractions import Fraction
from typing import Iterable, Optional

from .models import ConflictError, RejectError, UnknownReference
from .rules_service import canon

SCHEMA_VERSION = 1


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# Normalisation of one clerk's raw ranking grid
# --------------------------------------------------------------------------
def normalise_grid(grid) -> list[str]:
    """Normalise one transcription's grid exactly as the archive rule says.

    * empty cells are skipped;
    * a name that later appears *alone* again keeps its first occurrence;
    * the first cell that holds more than one name ends the sequence *before*
      that cell (a same-rank scribble truncates the rest).
    """
    if not isinstance(grid, (list, tuple)):
        raise RejectError("invalid_grid", "grid must be a list of cells")
    sequence: list[str] = []
    seen: set[str] = set()
    for cell in grid:
        if cell is None:
            continue
        if not isinstance(cell, (list, tuple)):
            raise RejectError("invalid_grid", "each cell must be a list of names")
        if len(cell) == 0:
            continue  # skip an empty cell
        if len(cell) > 1:
            break  # truncate before the first multi-person cell
        name = cell[0]
        if not isinstance(name, str) or not name:
            raise RejectError("invalid_grid", "cell names must be non-empty strings")
        if name in seen:
            continue  # keep only the first standalone occurrence
        seen.add(name)
        sequence.append(name)
    return sequence


def _frac(value: Fraction) -> str:
    """Exact textual fraction ("3", "1/4")."""
    return str(value)


# --------------------------------------------------------------------------
# SQLite-backed store for transcriptions, rulings and named ballot sets
# --------------------------------------------------------------------------
class BallotStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._depth = 0
        self._init_schema()

    @contextmanager
    def _tx(self):
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def _init_schema(self) -> None:
        with self._tx():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS transcriptions (
                    entry_id TEXT PRIMARY KEY,
                    original_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    clerk TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    grid_json TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transcription_key
                    ON transcriptions(original_id, clerk, revision);

                -- Append-only ruling revisions; an older revision stays readable.
                CREATE TABLE IF NOT EXISTS rulings (
                    ruling_id TEXT PRIMARY KEY,
                    original_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    sequence_json TEXT,
                    adjudicator TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ruling_revision
                    ON rulings(original_id, revision);

                CREATE TABLE IF NOT EXISTS ballot_sets (
                    set_id TEXT PRIMARY KEY,
                    original_ids_json TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ ingest
    def import_transcriptions(self, rows: Iterable[dict]) -> dict:
        rows = list(rows)
        parsed = []
        for i, raw in enumerate(rows):
            oid = raw.get("original_id")
            case_id = raw.get("case_id")
            clerk = raw.get("clerk")
            revision = raw.get("revision", 1)
            grid = raw.get("grid")
            if not isinstance(oid, str) or not oid:
                raise RejectError("invalid_transcription", "original_id required", i)
            if not isinstance(case_id, str) or not case_id:
                raise RejectError("invalid_transcription", "case_id required", i)
            if not isinstance(clerk, str) or not clerk:
                raise RejectError("invalid_transcription", "clerk required", i)
            if not _is_int(revision) or revision < 1:
                raise RejectError("invalid_transcription", "revision must be int >= 1", i)
            # Validate by normalising; this also proves every cell is well formed.
            normalise_grid(grid)
            payload = canon({"original_id": oid, "case_id": case_id, "clerk": clerk,
                             "revision": revision, "grid": grid})
            parsed.append((oid, case_id, clerk, revision, grid, payload))

        changed = False
        with self._tx():
            for oid, case_id, clerk, revision, grid, payload in parsed:
                row = self.conn.execute(
                    "SELECT canon FROM transcriptions WHERE original_id=? AND clerk=? "
                    "AND revision=?",
                    (oid, clerk, revision),
                ).fetchone()
                if row is not None:
                    if row["canon"] != payload:
                        raise ConflictError(
                            "transcription_conflict",
                            f"draft {oid}/{clerk} rev {revision} replayed with different content",
                        )
                    continue
                entry_id = f"{oid}:{clerk}:{revision}"
                self.conn.execute(
                    "INSERT INTO transcriptions(entry_id, original_id, case_id, clerk, "
                    "revision, grid_json, canon) VALUES (?,?,?,?,?,?,?)",
                    (entry_id, oid, case_id, clerk, revision,
                     json.dumps(grid, ensure_ascii=False), payload),
                )
                changed = True
        return {"accepted": len(parsed), "changed": changed}

    def import_rulings(self, rows: Iterable[dict]) -> dict:
        rows = list(rows)
        parsed = []
        for i, raw in enumerate(rows):
            oid = raw.get("original_id")
            revision = raw.get("revision")
            decision = raw.get("decision")
            sequence = raw.get("sequence")
            adjudicator = raw.get("adjudicator")
            reason = raw.get("reason")
            ruling_id = raw.get("ruling_id") or f"{oid}:r{revision}"
            if not isinstance(oid, str) or not oid:
                raise RejectError("invalid_ruling", "original_id required", i)
            if not _is_int(revision) or revision < 1:
                raise RejectError("invalid_ruling", "revision must be int >= 1", i)
            if decision not in ("valid", "spoiled"):
                raise RejectError("invalid_ruling", "decision must be 'valid' or 'spoiled'", i)
            if not isinstance(adjudicator, str) or not adjudicator:
                raise RejectError("invalid_ruling", "adjudicator required (traceability)", i)
            if not isinstance(reason, str) or not reason:
                raise RejectError("invalid_ruling", "reason required (traceability)", i)
            if decision == "valid":
                if not isinstance(sequence, (list, tuple)) or any(
                    not isinstance(x, str) or not x for x in sequence
                ):
                    raise RejectError(
                        "invalid_ruling", "valid ruling needs a list of names", i
                    )
                sequence = list(sequence)
            else:
                sequence = None
            if self.conn.execute(
                "SELECT 1 FROM transcriptions WHERE original_id=? LIMIT 1", (oid,)
            ).fetchone() is None:
                raise UnknownReference(
                    "unknown_ballot", f"ruling references unknown original ballot {oid}", i
                )
            payload = canon({"original_id": oid, "revision": revision,
                             "decision": decision, "sequence": sequence,
                             "adjudicator": adjudicator, "reason": reason})
            parsed.append((ruling_id, oid, revision, decision, sequence,
                           adjudicator, reason, payload))

        changed = False
        with self._tx():
            for ruling_id, oid, revision, decision, sequence, \
                    adjudicator, reason, payload in parsed:
                row = self.conn.execute(
                    "SELECT canon FROM rulings WHERE original_id=? AND revision=?",
                    (oid, revision),
                ).fetchone()
                if row is not None:
                    if row["canon"] != payload:
                        raise ConflictError(
                            "ruling_conflict",
                            f"ruling for {oid} rev {revision} replayed with different content",
                        )
                    continue
                max_rev = self.conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) AS m FROM rulings WHERE original_id=?",
                    (oid,),
                ).fetchone()["m"]
                if revision < max_rev:
                    raise ConflictError(
                        "stale_revision",
                        f"ruling for {oid} rev {revision} older than stored rev {max_rev}",
                    )
                self.conn.execute(
                    "INSERT INTO rulings(ruling_id, original_id, revision, decision, "
                    "sequence_json, adjudicator, reason, canon) VALUES (?,?,?,?,?,?,?,?)",
                    (ruling_id, oid, revision, decision,
                     None if sequence is None else json.dumps(sequence, ensure_ascii=False),
                     adjudicator, reason, payload),
                )
                changed = True
        return {"accepted": len(parsed), "changed": changed}

    def import_sets(self, rows: Iterable[dict]) -> dict:
        rows = list(rows)
        parsed = []
        for i, raw in enumerate(rows):
            set_id = raw.get("set_id")
            ids = raw.get("original_ids")
            if not isinstance(set_id, str) or not set_id:
                raise RejectError("invalid_set", "set_id required", i)
            if not isinstance(ids, list) or not ids or any(
                not isinstance(x, str) or not x for x in ids
            ):
                raise RejectError("invalid_set", "original_ids must be a non-empty list", i)
            if len(set(ids)) != len(ids):
                raise RejectError("invalid_set", "original_ids contains duplicates", i)
            for oid in ids:
                if self.conn.execute(
                    "SELECT 1 FROM transcriptions WHERE original_id=? LIMIT 1", (oid,)
                ).fetchone() is None:
                    raise UnknownReference(
                        "unknown_ballot", f"set {set_id} references unknown ballot {oid}", i
                    )
            parsed.append((set_id, ids, canon(sorted(ids))))

        changed = False
        with self._tx():
            for set_id, ids, payload in parsed:
                row = self.conn.execute(
                    "SELECT canon FROM ballot_sets WHERE set_id=?", (set_id,)
                ).fetchone()
                if row is not None:
                    if row["canon"] != payload:
                        raise ConflictError(
                            "set_conflict", f"ballot set {set_id} replayed with different content"
                        )
                    continue
                self.conn.execute(
                    "INSERT INTO ballot_sets(set_id, original_ids_json, canon) VALUES (?,?,?)",
                    (set_id, json.dumps(sorted(ids)), payload),
                )
                changed = True
        return {"accepted": len(parsed), "changed": changed}

    # ------------------------------------------------------------ snapshot
    def _drafts(self) -> dict[str, dict[str, dict]]:
        """original_id -> clerk -> latest draft row."""
        out: dict[str, dict[str, dict]] = {}
        for row in self.conn.execute(
            "SELECT * FROM transcriptions ORDER BY original_id, clerk, revision"
        ):
            oid = row["original_id"]
            by_clerk = out.setdefault(oid, {})
            current = by_clerk.get(row["clerk"])
            if current is None or row["revision"] >= current["revision"]:
                by_clerk[row["clerk"]] = {
                    "clerk": row["clerk"],
                    "case_id": row["case_id"],
                    "revision": row["revision"],
                    "grid": json.loads(row["grid_json"]),
                    "sequence": normalise_grid(json.loads(row["grid_json"])),
                }
        return out

    def _rulings(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in self.conn.execute(
            "SELECT * FROM rulings ORDER BY original_id, revision"
        ):
            out.setdefault(row["original_id"], []).append(
                {
                    "ruling_id": row["ruling_id"],
                    "original_id": row["original_id"],
                    "revision": row["revision"],
                    "decision": row["decision"],
                    "sequence": None if row["sequence_json"] is None
                    else json.loads(row["sequence_json"]),
                    "adjudicator": row["adjudicator"],
                    "reason": row["reason"],
                }
            )
        return out

    def _records(self) -> list[dict]:
        drafts = self._drafts()
        rulings = self._rulings()
        records = []
        for oid in sorted(set(drafts) | set(rulings)):
            by_clerk = drafts.get(oid, {})
            seqs = {clerk: d["sequence"] for clerk, d in sorted(by_clerk.items())}
            agreed = None
            if len(seqs) >= 2 and len({tuple(s) for s in seqs.values()}) == 1:
                agreed = next(iter(seqs.values()))
            history = rulings.get(oid, [])
            latest = history[-1] if history else None
            records.append(
                {
                    "original_id": oid,
                    "drafts": [by_clerk[c] for c in sorted(by_clerk)],
                    "draft_sequences": seqs,
                    "agreed_sequence": agreed,
                    "drafts_disagree": len(seqs) >= 2
                    and len({tuple(s) for s in seqs.values()}) > 1,
                    "rulings": history,
                    "latest_ruling": latest,
                }
            )
        return records

    # ------------------------------------------------------- reconciliation
    def reconcile(self) -> dict:
        """Status, effective sequence and dispute list for every original ballot."""
        ballots = []
        stats = {
            "originals": 0,
            "drafts": 0,
            "adopted_by_agreement": 0,
            "adjudicated": 0,
            "spoiled": 0,
            "disputed": 0,
            "incomplete": 0,
            "empty_sequence": 0,
            "with_first_choice": 0,
        }
        disputed = []
        for rec in self._records():
            stats["originals"] += 1
            stats["drafts"] += len(rec["drafts"])
            latest = rec["latest_ruling"]
            if latest is not None:
                if latest["decision"] == "valid":
                    effective = latest["sequence"]
                    status = "adjudicated"
                else:
                    effective = None
                    status = "spoiled"
            elif rec["agreed_sequence"] is not None:
                effective = rec["agreed_sequence"]
                status = "adopted"
            elif rec["drafts_disagree"]:
                effective = None
                status = "disputed"
            else:
                effective = None
                status = "incomplete"

            if status == "adopted":
                stats["adopted_by_agreement"] += 1
            elif status == "adjudicated":
                stats["adjudicated"] += 1
            elif status == "spoiled":
                stats["spoiled"] += 1
            elif status == "disputed":
                stats["disputed"] += 1
            else:
                stats["incomplete"] += 1
            if status in ("adopted", "adjudicated"):
                if effective == []:
                    stats["empty_sequence"] += 1
                elif len(effective) >= 1:
                    stats["with_first_choice"] += 1

            if rec["drafts_disagree"]:
                disputed.append(
                    {
                        "original_id": rec["original_id"],
                        "draft_sequences": rec["draft_sequences"],
                        "status": status,
                        "ruling_revision": None if latest is None else latest["revision"],
                        "effective_sequence": effective,
                    }
                )

            ballots.append(
                {
                    "original_id": rec["original_id"],
                    "drafts": [
                        {"clerk": d["clerk"], "case_id": d["case_id"],
                         "revision": d["revision"], "grid": d["grid"],
                         "sequence": d["sequence"]}
                        for d in rec["drafts"]
                    ],
                    "draft_sequences": rec["draft_sequences"],
                    "agreed_sequence": rec["agreed_sequence"],
                    "drafts_disagree": rec["drafts_disagree"],
                    "status": status,
                    "effective_sequence": effective,
                    "ruling": latest,
                    "ruling_history": rec["rulings"],
                }
            )
        return {"ballots": ballots, "stats": stats, "disputed": disputed}

    # --------------------------------------------------------------- export
    def _set_ids(self, set_id: str) -> list[str]:
        row = self.conn.execute(
            "SELECT original_ids_json FROM ballot_sets WHERE set_id=?", (set_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference("unknown_set", f"unknown ballot set {set_id}")
        return json.loads(row["original_ids_json"])

    def export(self, *, scope: str = "effective",
               original_ids: Optional[Iterable[str]] = None,
               set_id: Optional[str] = None,
               ruling_revision: Optional[int] = None) -> dict:
        """Export ballot sequences for independent recounting.

        ``scope='drafts'`` uses the agreed *transcription* (ignoring later
        review rulings), which makes the pre-correction record reproducible.
        ``scope='effective'`` applies the latest ruling at or below
        ``ruling_revision`` (default: latest), falling back to draft agreement.
        """
        if scope not in ("drafts", "effective"):
            raise RejectError("invalid_scope", "scope must be 'drafts' or 'effective'")
        records = {r["original_id"]: r for r in self._records()}
        if set_id is not None:
            ids = self._set_ids(set_id)
        elif original_ids is not None:
            ids = list(original_ids)
        else:
            ids = sorted(records)
        for oid in ids:
            if oid not in records:
                raise UnknownReference("unknown_ballot", f"unknown ballot {oid}")

        ballots = []
        excluded = []
        for oid in ids:
            rec = records[oid]
            if scope == "drafts":
                seq = rec["agreed_sequence"]
                reason = "no two-clerk agreement in transcriptions"
            else:
                history = rec["rulings"]
                if ruling_revision is not None:
                    history = [r for r in history if r["revision"] <= ruling_revision]
                ruling = history[-1] if history else None
                if ruling is not None:
                    if ruling["decision"] == "valid":
                        seq = ruling["sequence"]
                    else:
                        seq = None
                        reason = f"spoiled by ruling rev {ruling['revision']}"
                else:
                    seq = rec["agreed_sequence"]
                    reason = "disputed/unresolved" if rec["drafts_disagree"] \
                        else "no two-clerk agreement"
            if seq is None:
                excluded.append({"original_id": oid, "reason": reason})
            else:
                ballots.append({"ballot_id": oid, "sequence": list(seq)})

        empty = sum(1 for b in ballots if not b["sequence"])
        first = sum(1 for b in ballots if b["sequence"])
        return {
            "scope": scope,
            "set_id": set_id,
            "ruling_revision": ruling_revision,
            "ballots": ballots,
            "excluded": excluded,
            "stats": {
                "exported": len(ballots),
                "empty_sequence": empty,
                "with_first_choice": first,
                "excluded": len(excluded),
            },
        }


# --------------------------------------------------------------------------
# Exact transfer count (single transferable vote)
# --------------------------------------------------------------------------
def transfer_count(ballots, seats: int, priority) -> dict:
    """Count seats with exact fractions.

    ``ballots`` may be a list of sequences or the ``ballots`` array of an
    export (``{"ballot_id", "sequence"}``).  ``priority`` is the *complete*
    candidate priority order; earlier means higher priority, used to break
    ties both when electing and when eliminating.
    """
    if not _is_int(seats) or seats < 1:
        raise RejectError("invalid_seats", "seats must be int >= 1")
    if not isinstance(priority, (list, tuple)) or not priority:
        raise RejectError("invalid_priority", "priority must be a non-empty list")
    if any(not isinstance(c, str) or not c for c in priority):
        raise RejectError("invalid_priority", "priority entries must be non-empty strings")
    if len(set(priority)) != len(priority):
        raise RejectError("invalid_priority", "priority contains duplicates")
    prio = {c: i for i, c in enumerate(priority)}

    records = []
    for i, raw in enumerate(ballots):
        if isinstance(raw, dict):
            bid = raw.get("ballot_id", f"B{i}")
            seq = list(raw.get("sequence", []))
        else:
            bid = f"B{i}"
            seq = list(raw)
        for name in seq:
            if name not in prio:
                raise RejectError(
                    "unknown_candidate",
                    f"ballot {bid} names {name!r} outside the candidate priority order",
                )
        records.append(
            {
                "ballot_id": bid,
                "seq": seq,
                # A ballot without a first choice has no weight at all.
                "value": Fraction(1) if seq else Fraction(0),
                "ptr": 0,
            }
        )

    n_first = sum(1 for r in records if r["value"] > 0)
    quota = Fraction(n_first // (seats + 1) + 1)

    continuing = set(priority)
    elected: list[str] = []
    exhausted_total = Fraction(0)
    rounds: list[dict] = []

    def next_pref(record):
        j = record["ptr"]
        seq = record["seq"]
        while j < len(seq):
            if seq[j] in continuing:
                return j, seq[j]
            j += 1
        return None, None

    while len(elected) < seats and continuing:
        totals = {c: Fraction(0) for c in priority if c in continuing}
        assigned: dict[str, list[tuple[dict, int]]] = {}
        for record in records:
            if record["value"] == 0:
                continue
            j, cand = next_pref(record)
            if cand is None:
                exhausted_total += record["value"]
                record["value"] = Fraction(0)
                record["ptr"] = len(record["seq"])
                continue
            totals[cand] += record["value"]
            assigned.setdefault(cand, []).append((record, j))

        round_rec = {
            "round": len(rounds) + 1,
            "totals": {c: _frac(totals[c]) for c in priority if c in continuing},
            "continuing": [c for c in priority if c in continuing],
        }

        reached = [c for c in continuing if totals[c] >= quota]
        if reached:
            # Highest total wins; ties go to the earlier priority.
            cand = min(reached, key=lambda c: (-totals[c], prio[c]))
            total = totals[cand]
            surplus = total - quota
            continuing.discard(cand)
            elected.append(cand)
            transferred = Fraction(0)
            exhausted = Fraction(0)
            transfers = []
            for record, j in assigned.get(cand, []):
                value = record["value"]
                new_value = (value * surplus / total) if surplus > 0 else Fraction(0)
                record["value"] = new_value
                record["ptr"] = j + 1
                transferred += new_value
                _, target = next_pref(record)
                if target is None:
                    if new_value > 0:
                        exhausted += new_value
                        record["value"] = Fraction(0)
                        record["ptr"] = len(record["seq"])
                transfers.append(
                    {
                        "ballot_id": record["ballot_id"],
                        "from": cand,
                        "to": target,
                        "value": _frac(new_value),
                        "exhausted": target is None and new_value > 0,
                    }
                )
            exhausted_total += exhausted
            round_rec.update(
                action="elect",
                candidate=cand,
                elected=list(elected),
                retained=_frac(total - transferred),  # the quota kept
                transferred=_frac(transferred),        # the surplus leaving, incl. exhausted
                exhausted=_frac(exhausted),            # surplus with no successor
                transfers=transfers,
            )
        elif len(continuing) <= seats - len(elected):
            # Exactly enough remaining candidates for the unfilled seats.
            newly = [c for c in priority if c in continuing]
            elected.extend(newly)
            continuing.clear()
            round_rec.update(
                action="elect_remaining",
                candidate=None,
                elected=list(elected),
                retained="0",
                transferred="0",
                exhausted="0",
                transfers=[],
            )
        else:
            # Lowest total is eliminated; ties go to the later priority.
            cand = min(continuing, key=lambda c: (totals[c], -prio[c]))
            total = totals[cand]
            continuing.discard(cand)
            transferred = Fraction(0)
            exhausted = Fraction(0)
            transfers = []
            for record, j in assigned.get(cand, []):
                value = record["value"]
                transferred += value
                record["ptr"] = j + 1
                _, target = next_pref(record)
                if target is None:
                    exhausted += value
                    record["value"] = Fraction(0)
                    record["ptr"] = len(record["seq"])
                transfers.append(
                    {
                        "ballot_id": record["ballot_id"],
                        "from": cand,
                        "to": target,
                        "value": _frac(value),
                        "exhausted": target is None,
                    }
                )
            exhausted_total += exhausted
            round_rec.update(
                action="eliminate",
                candidate=cand,
                elected=list(elected),
                retained="0",
                transferred=_frac(transferred),  # full weight moved on
                exhausted=_frac(exhausted),
                transfers=transfers,
            )
        rounds.append(round_rec)

    return {
        "seats": seats,
        "priority": list(priority),
        "ballots": len(records),
        "n_first_choice": n_first,
        "quota": _frac(quota),
        "elected": elected,
        "not_elected": [c for c in priority if c not in elected],
        "exhausted_total": _frac(exhausted_total),
        "rounds": rounds,
    }


# --------------------------------------------------------------------------
# Materials loader and documented demonstration
# --------------------------------------------------------------------------
def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_ballot_materials(directory: str) -> dict:
    transcriptions = _read_jsonl(os.path.join(directory, "transcriptions.jsonl"))
    rulings = []
    rpath = os.path.join(directory, "rulings.jsonl")
    if os.path.exists(rpath):
        rulings = _read_jsonl(rpath)
    sets = []
    spath = os.path.join(directory, "sets.json")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as fh:
            sets = json.load(fh).get("sets", [])
    return {"transcriptions": transcriptions, "rulings": rulings, "sets": sets}


def open_ballot_store(data_dir: str) -> "BallotStore":
    return BallotStore(os.path.join(data_dir, "ballots.sqlite"))


def run_ballot_demo(data_dir: str, materials: str = "materials/ballots") -> dict:
    """Reconcile the fictional ballots and run the three documented counts."""
    os.makedirs(data_dir, exist_ok=True)
    db = os.path.join(data_dir, "ballots.sqlite")
    if os.path.exists(db):
        os.remove(db)
    store = BallotStore(db)
    artifacts = os.path.join(data_dir, "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    steps: list[dict] = []
    ok = True

    raw = load_ballot_materials(materials)
    imported = {
        "transcriptions": store.import_transcriptions(raw["transcriptions"]),
        "rulings": store.import_rulings(raw["rulings"]),
        "sets": store.import_sets(raw["sets"]),
    }
    steps.append({"step": "import", "result": imported})

    rec = store.reconcile()
    steps.append({"step": "reconcile", "stats": rec["stats"],
                  "disputed": rec["disputed"]})
    by_id = {b["original_id"]: b for b in rec["ballots"]}
    steps.append({"step": "same_rank_ballot", "original_id": "O8",
                  "drafts": [d["sequence"] for d in by_id["O8"]["drafts"]],
                  "effective": by_id["O8"]["effective_sequence"],
                  "note": "multi-name cell truncated before that cell"})
    steps.append({"step": "duplicate_name_ballot", "original_id": "O9",
                  "effective": by_id["O9"]["effective_sequence"],
                  "note": "repeated standalone name kept once"})
    steps.append({"step": "empty_ballot", "original_id": "O12",
                  "effective": by_id["O12"]["effective_sequence"]})
    steps.append({"step": "historical_ruling", "original_id": "O10",
                  "history": [
                      {"revision": r["revision"], "decision": r["decision"],
                       "sequence": r["sequence"], "adjudicator": r["adjudicator"]}
                      for r in by_id["O10"]["ruling_history"]
                  ]})

    def write(name, obj):
        path = os.path.join(artifacts, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return path

    # Pre-correction transcription and post-review effective ballots, both
    # exported as standalone files so a count can be recomputed independently.
    original_export = store.export(scope="drafts", set_id="seven")
    corrected_export = store.export(scope="effective", set_id="seven")
    original_path = write("ballots_original.json", original_export)
    corrected_path = write("ballots_corrected.json", corrected_export)
    steps.append({"step": "export", "original": original_path,
                  "corrected": corrected_path,
                  "original_stats": original_export["stats"],
                  "corrected_stats": corrected_export["stats"]})

    # Three documented counts.
    count_original = transfer_count(original_export["ballots"], 2, ["A", "B", "C"])
    count_corrected_acb = transfer_count(corrected_export["ballots"], 2, ["A", "C", "B"])
    count_corrected_abc = transfer_count(corrected_export["ballots"], 2, ["A", "B", "C"])
    write("count_original.json", count_original)
    write("count_corrected_ACB.json", count_corrected_acb)
    write("count_corrected_ABC.json", count_corrected_abc)
    steps.append({"step": "count_original", "elected": count_original["elected"],
                  "quota": count_original["quota"],
                  "rounds": count_original["rounds"]})
    steps.append({"step": "count_corrected_ACB", "elected": count_corrected_acb["elected"],
                  "quota": count_corrected_acb["quota"],
                  "rounds": count_corrected_acb["rounds"],
                  "exhausted_total": count_corrected_acb["exhausted_total"]})
    steps.append({"step": "count_corrected_ABC", "elected": count_corrected_abc["elected"],
                  "quota": count_corrected_abc["quota"],
                  "rounds": count_corrected_abc["rounds"],
                  "exhausted_total": count_corrected_abc["exhausted_total"]})

    # Independent recount from the written files (no store involved).
    recounts = {}
    for label, path, priority in (
        ("original", original_path, ["A", "B", "C"]),
        ("corrected_ACB", corrected_path, ["A", "C", "B"]),
        ("corrected_ABC", corrected_path, ["A", "B", "C"]),
    ):
        with open(path, encoding="utf-8") as fh:
            exported = json.load(fh)
        recounts[label] = transfer_count(exported["ballots"], 2, priority)
    steps.append({"step": "independent_recount", "results": {
        k: v["elected"] for k, v in recounts.items()}})

    # Round detail checks: corrected ACB exposes an exhausted transfer.
    acb_rounds = count_corrected_acb["rounds"]
    exhaustion_round = next(
        (r for r in acb_rounds if r["action"] == "eliminate" and r["exhausted"] != "0"),
        None,
    )
    retained_after_surplus = next(
        (r["retained"] for r in acb_rounds if r["action"] == "elect"), None
    )

    expected = {
        "dedup_by_original": rec["stats"]["originals"] == 12,
        "same_rank_truncated": by_id["O8"]["effective_sequence"] == ["A"],
        "duplicate_kept_once": by_id["O9"]["effective_sequence"] == ["A", "B", "C"],
        "empty_sequence_retained": rec["stats"]["empty_sequence"] >= 1,
        "with_first_choice_retained": rec["stats"]["with_first_choice"] >= 7,
        "dispute_O10_resolved": by_id["O10"]["effective_sequence"] == ["A", "C"],
        "dispute_O11_spoiled": by_id["O11"]["status"] == "spoiled",
        "old_ruling_readable": [r["sequence"] for r in by_id["O10"]["ruling_history"]]
        == [["A", "B"], ["A", "C"]],
        "original_elects_A_B": count_original["elected"] == ["A", "B"],
        "corrected_ACB_elects_A_C": count_corrected_acb["elected"] == ["A", "C"],
        "corrected_ABC_elects_A_B": count_corrected_abc["elected"] == ["A", "B"],
        "corrected_ACB_retains_quota": retained_after_surplus == "3",
        "corrected_ACB_has_exhaustion": exhaustion_round is not None
        and exhaustion_round["exhausted"] == "2",
        "independent_recount_matches": (
            recounts["original"]["elected"] == count_original["elected"]
            and recounts["corrected_ACB"]["elected"] == count_corrected_acb["elected"]
            and recounts["corrected_ABC"]["elected"] == count_corrected_abc["elected"]
        ),
    }
    ok = all(expected.values())
    result = {
        "ok": ok,
        "expected": expected,
        "materials": os.path.abspath(materials),
        "data_dir": os.path.abspath(data_dir),
        "steps": steps,
    }
    write("ballot_demo.json", result)
    store.close()
    return result
