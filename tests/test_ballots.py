"""Unittest suite for ballot reconciliation and exact transfer counting."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from archive_handoff.ballots import (BallotStore, load_ballot_materials,
                                     normalise_grid, run_ballot_demo, transfer_count)
from archive_handoff.models import ConflictError, RejectError, UnknownReference

MATERIALS = "materials/ballots"


def build(tmp):
    store = BallotStore(os.path.join(tmp, "ballots.db"))
    raw = load_ballot_materials(MATERIALS)
    store.import_transcriptions(raw["transcriptions"])
    store.import_rulings(raw["rulings"])
    store.import_sets(raw["sets"])
    return store


class Normalisation(unittest.TestCase):
    def test_skip_empty_cells(self):
        self.assertEqual(normalise_grid([[], ["A"], [], ["B"]]), ["A", "B"])

    def test_duplicate_standalone_kept_once(self):
        self.assertEqual(normalise_grid([["A"], ["B"], ["A"], ["C"]]), ["A", "B", "C"])

    def test_multi_name_cell_truncates_before_cell(self):
        self.assertEqual(normalise_grid([["A"], ["B", "C"], ["D"]]), ["A"])

    def test_first_multi_name_cell_wins(self):
        self.assertEqual(normalise_grid([["A"], [], ["B"], ["C", "D"]]), ["A", "B"])

    def test_empty_grid(self):
        self.assertEqual(normalise_grid([[], []]), [])

    def test_bad_grid_rejected(self):
        with self.assertRaises(RejectError):
            normalise_grid("A>B")
        with self.assertRaises(RejectError):
            normalise_grid([[3]])


class Reconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build(self.tmp.name)
        self.addCleanup(self.store.close)
        self.rec = self.store.reconcile()
        self.by_id = {b["original_id"]: b for b in self.rec["ballots"]}

    def test_stable_original_counted_once_across_cases(self):
        # O1 has two drafts in different cases (K1/K2); it is still one ballot.
        self.assertEqual(self.rec["stats"]["originals"], 12)
        self.assertEqual(len(self.by_id["O1"]["drafts"]), 2)
        self.assertEqual({d["case_id"] for d in self.by_id["O1"]["drafts"]},
                         {"K1", "K2"})

    def test_agreement_adopts(self):
        # O5 has agreeing drafts and no ruling: adopted by transcription.
        self.assertEqual(self.by_id["O5"]["status"], "adopted")
        self.assertEqual(self.by_id["O5"]["effective_sequence"], ["B"])

    def test_ruling_overrides_transcription(self):
        # Reviewed correction turns the four A>B into A>C.
        for oid in ("O1", "O2", "O3", "O4"):
            self.assertEqual(self.by_id[oid]["status"], "adjudicated")
            self.assertEqual(self.by_id[oid]["effective_sequence"], ["A", "C"])

    def test_conflicting_drafts_are_disputed(self):
        self.assertTrue(self.by_id["O10"]["drafts_disagree"])
        self.assertTrue(self.by_id["O11"]["drafts_disagree"])
        disputed_ids = {d["original_id"] for d in self.rec["disputed"]}
        self.assertEqual(disputed_ids, {"O10", "O11"})

    def test_old_ruling_still_readable(self):
        hist = self.by_id["O10"]["ruling_history"]
        self.assertEqual([r["revision"] for r in hist], [1, 2])
        self.assertEqual(hist[0]["sequence"], ["A", "B"])
        self.assertEqual(hist[0]["adjudicator"], "L-MIRA")
        self.assertEqual(self.by_id["O10"]["effective_sequence"], ["A", "C"])

    def test_spoiled_ballot(self):
        self.assertEqual(self.by_id["O11"]["status"], "spoiled")
        self.assertIsNone(self.by_id["O11"]["effective_sequence"])
        self.assertEqual(self.rec["stats"]["spoiled"], 1)

    def test_empty_and_first_choice_counts(self):
        self.assertEqual(self.by_id["O12"]["effective_sequence"], [])
        self.assertEqual(self.rec["stats"]["empty_sequence"], 1)
        self.assertEqual(self.rec["stats"]["with_first_choice"], 10)

    def test_duplicate_import_is_idempotent(self):
        raw = load_ballot_materials(MATERIALS)
        again = self.store.import_transcriptions(raw["transcriptions"])
        self.assertFalse(again["changed"])

    def test_conflicting_redraft_rejected(self):
        with self.assertRaises(ConflictError):
            self.store.import_transcriptions([
                {"original_id": "O1", "case_id": "K1", "clerk": "甲",
                 "revision": 1, "grid": [["A"], ["C"]]},
            ])

    def test_stale_ruling_revision_rejected(self):
        with self.assertRaises(ConflictError):
            self.store.import_rulings([
                {"original_id": "O10", "revision": 1, "decision": "valid",
                 "sequence": ["A"], "adjudicator": "L-X", "reason": "viejo"},
            ])

    def test_ruling_needs_traceability(self):
        with self.assertRaises(RejectError):
            self.store.import_rulings([
                {"original_id": "O10", "revision": 3, "decision": "valid",
                 "sequence": ["A"], "adjudicator": "", "reason": "x"},
            ])

    def test_ruling_unknown_ballot(self):
        with self.assertRaises(UnknownReference):
            self.store.import_rulings([
                {"original_id": "NOPE", "revision": 1, "decision": "spoiled",
                 "adjudicator": "L-X", "reason": "x"},
            ])


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build(self.tmp.name)
        self.addCleanup(self.store.close)

    def test_scope_drafts_ignores_rulings(self):
        exp = self.store.export(scope="drafts", set_id="seven")
        self.assertEqual(len(exp["ballots"]), 7)
        self.assertEqual(exp["ballots"][0]["sequence"], ["A", "B"])

    def test_scope_effective_applies_latest(self):
        exp = self.store.export(scope="effective", set_id="seven")
        # 4x A>C, 2x B, 1x C after the correction.
        seconds = sorted(b["sequence"][1] for b in exp["ballots"]
                         if len(b["sequence"]) > 1)
        self.assertEqual(seconds, ["C", "C", "C", "C"])
        self.assertEqual(sum(1 for b in exp["ballots"] if b["sequence"] == ["B"]), 2)
        self.assertEqual(sum(1 for b in exp["ballots"] if b["sequence"] == ["C"]), 1)

    def test_ruling_revision_pins_old_decision(self):
        # Export every ballot (no set) and pin O10 to its first ruling.
        exp = self.store.export(scope="effective", ruling_revision=1)
        o10 = next(b for b in exp["ballots"] if b["ballot_id"] == "O10")
        self.assertEqual(o10["sequence"], ["A", "B"])

    def test_export_is_standalone_json(self):
        exp = self.store.export(scope="effective", set_id="seven")
        text = json.dumps(exp)
        reloaded = json.loads(text)
        self.assertEqual(reloaded, exp)


class TransferCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build(self.tmp.name)
        self.addCleanup(self.store.close)

    def _ballots(self, scope="drafts"):
        return self.store.export(scope=scope, set_id="seven")["ballots"]

    def test_original_elects_a_and_b(self):
        result = transfer_count(self._ballots("drafts"), 2, ["A", "B", "C"])
        self.assertEqual(result["quota"], "3")
        self.assertEqual(result["elected"], ["A", "B"])
        # A's four ballots each pass exactly 1/4 to B.
        first = result["rounds"][0]
        self.assertEqual(first["action"], "elect")
        self.assertEqual(first["candidate"], "A")
        self.assertEqual(first["retained"], "3")
        self.assertEqual(first["transferred"], "1")
        self.assertEqual([t["value"] for t in first["transfers"]],
                         ["1/4", "1/4", "1/4", "1/4"])
        self.assertEqual(result["exhausted_total"], "0")

    def test_corrected_priority_acb_elects_a_and_c(self):
        result = transfer_count(self._ballots("effective"), 2, ["A", "C", "B"])
        self.assertEqual(result["elected"], ["A", "C"])
        # Surplus reaches C, B is eliminated and its full weight is exhausted.
        eliminate = next(r for r in result["rounds"] if r["action"] == "eliminate")
        self.assertEqual(eliminate["candidate"], "B")
        self.assertEqual(eliminate["transferred"], "2")
        self.assertEqual(eliminate["exhausted"], "2")
        self.assertEqual(result["exhausted_total"], "2")

    def test_corrected_priority_abc_elects_a_and_b(self):
        result = transfer_count(self._ballots("effective"), 2, ["A", "B", "C"])
        self.assertEqual(result["elected"], ["A", "B"])
        self.assertEqual(result["exhausted_total"], "2")

    def test_quota_uses_first_choices_not_ballots(self):
        ballots = [{"ballot_id": "x", "sequence": []},
                   {"ballot_id": "y", "sequence": ["A"]},
                   {"ballot_id": "z", "sequence": ["A", "B"]}]
        result = transfer_count(ballots, 1, ["A", "B"])
        self.assertEqual(result["n_first_choice"], 2)
        self.assertEqual(result["quota"], "2")  # floor(2/2)+1

    def test_elect_remaining_when_candidates_match_seats(self):
        ballots = [["A"], ["B"], ["C"]]
        result = transfer_count(ballots, 2, ["A", "B", "C"])
        # 3 candidates / 2 seats, 1 each: C (later priority) eliminated, then
        # the remaining A and B fill the two seats.
        self.assertEqual(result["elected"], ["A", "B"])
        self.assertEqual(result["rounds"][-1]["action"], "elect_remaining")

    def test_zero_surplus_transfers_nothing(self):
        ballots = [["A"], ["A"], ["A"], ["B"]]
        result = transfer_count(ballots, 1, ["A", "B"])
        # N=4 -> quota 3; A exactly meets it with a single surplus.
        first = result["rounds"][0]
        self.assertEqual(first["retained"], "3")
        self.assertEqual(first["transferred"], "0")
        self.assertEqual(result["elected"], ["A"])

    def test_tie_break_by_priority(self):
        # A and B tie at 2; elect uses earlier priority, eliminate the later.
        ballots = [["A"], ["B"], ["C"], ["A"], ["B"], ["C"]]
        result = transfer_count(ballots, 2, ["A", "B", "C"])
        self.assertEqual(result["elected"], ["A", "B"])
        self.assertEqual(result["rounds"][-1]["action"], "elect_remaining")

    def test_unknown_candidate_rejected(self):
        with self.assertRaises(RejectError):
            transfer_count([["A", "X"]], 1, ["A", "B"])

    def test_duplicate_priority_rejected(self):
        with self.assertRaises(RejectError):
            transfer_count([["A"]], 1, ["A", "A"])


class DocumentedBallotDemo(unittest.TestCase):
    def test_demo_ok(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_ballot_demo(os.path.join(tmp.name, "demo"))
        self.assertTrue(result["ok"], json.dumps(result.get("expected"), indent=2))
        for label, holds in result["expected"].items():
            self.assertTrue(holds, f"documented expectation failed: {label}")
        artifacts = os.path.join(tmp.name, "demo", "artifacts")
        for name in ("ballots_original.json", "ballots_corrected.json",
                     "count_original.json", "count_corrected_ACB.json",
                     "count_corrected_ABC.json", "ballot_demo.json"):
            self.assertTrue(os.path.exists(os.path.join(artifacts, name)), name)


if __name__ == "__main__":
    unittest.main()
