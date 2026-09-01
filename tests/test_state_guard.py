from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "state_guard.py"
SPEC = importlib.util.spec_from_file_location("state_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
state_guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = state_guard
SPEC.loader.exec_module(state_guard)


class StateGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "state.json"
        self.document = state_guard.new_state(
            task_id="task-42",
            owner="root",
            objective="Deliver verified change",
            criteria={"tests": "Required tests pass", "review": "Review passes"},
            updated_at="2026-09-01T12:00:00Z",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialize(self) -> dict:
        return state_guard.init_state(self.path, self.document)

    def assert_rejected_unchanged(self, code: str, operation) -> state_guard.StateGuardError:
        before = self.path.read_bytes()
        with self.assertRaises(state_guard.StateGuardError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.path.read_bytes(), before)
        return caught.exception

    def apply(self, patch: dict, *, revision: int = 1, **kwargs):
        return state_guard.apply_state(
            self.path,
            patch,
            task_id=kwargs.pop("task_id", "task-42"),
            actor=kwargs.pop("actor", "root"),
            expected_revision=revision,
            **kwargs,
        )

    def test_01_init_check_and_no_overwrite(self) -> None:
        initialized = self.initialize()
        self.assertEqual(initialized["state_revision"], 1)
        checked = state_guard.check_state(self.path, task_id="task-42", owner="root")
        self.assertEqual(checked, initialized)
        self.assert_rejected_unchanged(
            "ALREADY_EXISTS", lambda: state_guard.init_state(self.path, self.document)
        )

    def test_02_happy_path_increments_once(self) -> None:
        self.initialize()
        with mock.patch.object(state_guard, "_utc_now", return_value="2026-09-01T12:00:01Z"):
            result = self.apply({"facts": {"tests": "passed"}})
        self.assertEqual(result["state_revision"], 2)
        self.assertEqual(result["facts"], {"tests": "passed"})
        self.assertEqual(result["updated_at"], "2026-09-01T12:00:01Z")

    def test_03_recursive_merge_list_replace_and_null_delete(self) -> None:
        self.initialize()
        first = self.apply(
            {
                "facts": {"build": {"status": "old", "keep": 7}},
                "pending": ["one", "two"],
            }
        )
        second = self.apply(
            {
                "facts": {"build": {"status": "new", "keep": None}},
                "pending": ["three"],
            },
            revision=first["state_revision"],
        )
        self.assertEqual(second["facts"], {"build": {"status": "new"}})
        self.assertEqual(second["pending"], ["three"])

    def test_04_required_and_protected_fields_are_guarded(self) -> None:
        self.initialize()
        self.assert_rejected_unchanged(
            "PROTECTED_FIELD", lambda: self.apply({"task_id": "other"})
        )
        self.assert_rejected_unchanged(
            "MISSING_FIELD", lambda: self.apply({"facts": None})
        )

    def test_05_stale_cas_prevents_lost_update(self) -> None:
        self.initialize()
        first = self.apply({"facts": {"winner": 1}}, revision=1)
        self.assertEqual(first["state_revision"], 2)
        self.assert_rejected_unchanged(
            "STALE_REVISION",
            lambda: self.apply({"facts": {"loser": 2}}, revision=1),
        )
        current = state_guard.load_state(self.path)
        self.assertEqual(current["facts"], {"winner": 1})

    def test_06_task_mismatch_is_rejected(self) -> None:
        self.initialize()
        self.assert_rejected_unchanged(
            "TASK_MISMATCH",
            lambda: self.apply({"facts": {"x": 1}}, task_id="task-99"),
        )

    def test_07_wrong_owner_is_rejected(self) -> None:
        self.initialize()
        self.assert_rejected_unchanged(
            "OWNER_MISMATCH",
            lambda: self.apply({"facts": {"x": 1}}, actor="worker"),
        )

    def test_08_strict_json_rejects_duplicate_and_nonobject_root(self) -> None:
        self.path.write_bytes(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaises(state_guard.StateGuardError) as duplicate:
            state_guard.load_state(self.path)
        self.assertEqual(duplicate.exception.code, "DUPLICATE_KEY")
        self.path.write_bytes(b"[]")
        with self.assertRaises(state_guard.StateGuardError) as root:
            state_guard.load_state(self.path)
        self.assertEqual(root.exception.code, "INVALID_ROOT")

    def test_09_types_null_and_nonfinite_numbers_are_rejected(self) -> None:
        invalid = dict(self.document)
        invalid["state_revision"] = True
        with self.assertRaises(state_guard.StateGuardError) as boolean:
            state_guard.validate_state(invalid)
        self.assertEqual(boolean.exception.code, "INVALID_FIELD")
        invalid = dict(self.document)
        invalid["facts"] = {"null": None}
        with self.assertRaises(state_guard.StateGuardError) as null:
            state_guard.validate_state(invalid)
        self.assertEqual(null.exception.code, "NULL_NOT_STORABLE")
        self.path.write_bytes(b'{"value":1e9999}')
        with self.assertRaises(state_guard.StateGuardError) as nonfinite:
            parsed = state_guard._decode_object(
                state_guard._read_bytes(self.path, state_guard.DEFAULT_MAX_BYTES), "state"
            )
            state_guard._walk_json(parsed)
        self.assertEqual(nonfinite.exception.code, "NON_FINITE_NUMBER")

    def test_10_utf8_byte_limit_is_enforced_atomically(self) -> None:
        self.initialize()
        self.assert_rejected_unchanged(
            "STATE_TOO_LARGE",
            lambda: self.apply(
                {"facts": {"multibyte": "я" * 900}},
                max_bytes=1024,
            ),
        )

    def test_11_replace_failure_preserves_old_bytes_and_cleans_temp(self) -> None:
        self.initialize()
        with mock.patch.object(state_guard.os, "replace", side_effect=OSError("blocked")):
            self.assert_rejected_unchanged(
                "ATOMIC_WRITE_FAILED", lambda: self.apply({"facts": {"x": 1}})
            )
        leftovers = list(self.root.glob(".state.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_12_handoff_is_atomic_and_old_owner_loses_authority(self) -> None:
        self.initialize()
        handed = state_guard.handoff_state(
            self.path,
            task_id="task-42",
            actor="root",
            new_owner="reviewer",
            expected_revision=1,
        )
        self.assertEqual(handed["owner"], "reviewer")
        self.assertEqual(handed["state_revision"], 2)
        self.assert_rejected_unchanged(
            "OWNER_MISMATCH",
            lambda: self.apply({"facts": {"x": 1}}, revision=2),
        )
        updated = state_guard.apply_state(
            self.path,
            {"facts": {"reviewed": True}},
            task_id="task-42",
            actor="reviewer",
            expected_revision=2,
        )
        self.assertEqual(updated["state_revision"], 3)

    def test_13_objective_revision_invalidates_stale_evidence(self) -> None:
        self.initialize()
        passed = self.apply(
            {
                "success_criteria": {
                    "tests": {
                        "status": "passed",
                        "evidence_ref": {
                            "ref": "pytest",
                            "objective_revision": 1,
                            "scope": "commit:abc",
                        },
                    }
                }
            }
        )
        self.assertEqual(passed["success_criteria"]["tests"]["status"], "passed")
        self.assert_rejected_unchanged(
            "STALE_EVIDENCE",
            lambda: self.apply(
                {"objective": "Deliver revised verified change"},
                revision=2,
                new_objective_revision=2,
            ),
        )
        revised = self.apply(
            {
                "objective": "Deliver revised verified change",
                "success_criteria": {
                    "tests": {"status": "unknown", "evidence_ref": None}
                },
            },
            revision=2,
            new_objective_revision=2,
        )
        self.assertEqual(revised["objective_revision"], 2)
        self.assertNotIn("evidence_ref", revised["success_criteria"]["tests"])

    def test_14_noop_does_not_rewrite_or_bump(self) -> None:
        self.initialize()
        before = self.path.read_bytes()
        before_mtime = self.path.stat().st_mtime_ns
        result = self.apply({"facts": {"missing": None}})
        self.assertEqual(result["state_revision"], 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, before_mtime)

    def test_15_completion_requires_current_evidence(self) -> None:
        self.initialize()
        self.assertEqual(len(state_guard.completion_errors(self.document)), 2)
        patch = {"success_criteria": {}}
        for criterion_id in self.document["success_criteria"]:
            patch["success_criteria"][criterion_id] = {
                "status": "passed",
                "evidence_ref": {
                    "ref": f"proof:{criterion_id}",
                    "objective_revision": 1,
                    "scope": "commit:abc",
                },
            }
        result = self.apply(patch)
        self.assertEqual(state_guard.completion_errors(result), [])

    def test_16_cli_smoke_and_machine_readable_error(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT),
            "init",
            "cli-state.json",
            "--task-id",
            "cli-task",
            "--owner",
            "root",
            "--objective",
            "Validate CLI",
            "--criterion",
            "done=CLI succeeds",
        ]
        initialized = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertTrue(json.loads(initialized.stdout)["ok"])
        duplicate = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(duplicate.returncode, state_guard.EXIT_CONFLICT)
        self.assertEqual(json.loads(duplicate.stderr)["code"], "ALREADY_EXISTS")

    def test_17_object_patch_cannot_reinterpret_scalar(self) -> None:
        self.initialize()
        first = self.apply({"facts": {"typed": "scalar"}})
        self.assert_rejected_unchanged(
            "TYPE_CONFLICT",
            lambda: self.apply(
                {"facts": {"typed": {"now": "object"}}},
                revision=first["state_revision"],
            ),
        )

    def test_18_post_replace_fsync_failure_is_commit_uncertain(self) -> None:
        self.initialize()
        uncertain = state_guard.StateGuardError(
            "COMMIT_UNCERTAIN",
            "directory durability could not be confirmed",
            state_guard.EXIT_IO,
        )
        with mock.patch.object(state_guard, "_fsync_directory", side_effect=uncertain):
            with self.assertRaises(state_guard.StateGuardError) as caught:
                self.apply({"facts": {"committed": True}})
        self.assertEqual(caught.exception.code, "COMMIT_UNCERTAIN")
        self.assertTrue(state_guard.load_state(self.path)["facts"]["committed"])

    def test_19_cross_process_sidecar_lock_times_out(self) -> None:
        self.initialize()
        holder_code = """
import importlib.util
import sys
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location('holder_guard', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
with module._FileLock(Path(sys.argv[2]), 1.0):
    print('READY', flush=True)
    time.sleep(0.8)
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(SCRIPT), str(self.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "READY")
            self.assert_rejected_unchanged(
                "LOCK_TIMEOUT",
                lambda: self.apply(
                    {"facts": {"blocked": True}},
                    lock_timeout=0.1,
                ),
            )
        finally:
            _, stderr = holder.communicate(timeout=3)
        self.assertEqual(holder.returncode, 0, stderr)

    def test_20_cli_paths_are_confined_to_trusted_root(self) -> None:
        with self.assertRaises(state_guard.StateGuardError) as caught:
            state_guard._within_root(self.root.parent / "outside.json", self.root)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")

    def test_21_hard_linked_state_is_rejected(self) -> None:
        self.initialize()
        linked = self.root / "linked.json"
        try:
            os.link(self.path, linked)
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")
        with self.assertRaises(state_guard.StateGuardError) as caught:
            state_guard.load_state(self.path)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")

    def test_22_excessive_patch_depth_is_rejected_unchanged(self) -> None:
        self.initialize()
        nested: object = "leaf"
        for _ in range(state_guard.MAX_DEPTH + 2):
            nested = {"next": nested}
        self.assert_rejected_unchanged(
            "NESTING_TOO_DEEP",
            lambda: self.apply({"facts": {"deep": nested}}),
        )

    def test_23_requirement_changes_need_new_objective_revision(self) -> None:
        self.initialize()
        prepared = self.apply(
            {
                "success_criteria": {
                    "tests": {
                        "status": "passed",
                        "evidence_ref": {
                            "ref": "proof:tests",
                            "objective_revision": 1,
                            "scope": "commit:abc",
                        },
                    },
                    "review": {
                        "status": "failed",
                        "evidence_ref": {
                            "ref": "proof:review",
                            "objective_revision": 1,
                            "scope": "commit:abc",
                        },
                    },
                }
            }
        )
        revision = prepared["state_revision"]
        for patch in (
            {"success_criteria": {"tests": {"statement": "A different requirement"}}},
            {"success_criteria": {"review": None}},
            {
                "success_criteria": {
                    "security": {"statement": "Security passes", "status": "unknown"}
                }
            },
        ):
            with self.subTest(patch=patch):
                self.assert_rejected_unchanged(
                    "OBJECTIVE_REVISION_REQUIRED",
                    lambda patch=patch: self.apply(patch, revision=revision),
                )
        self.assertFalse(state_guard.completion_errors(state_guard.load_state(self.path)) == [])

    def test_24_nonstring_status_is_structured_validation_error(self) -> None:
        self.initialize()
        self.assert_rejected_unchanged(
            "INVALID_STATUS",
            lambda: self.apply({"success_criteria": {"tests": {"status": []}}}),
        )

    def test_25_lock_release_failure_is_reconcile_needed_and_does_not_mask(self) -> None:
        self.initialize()
        with mock.patch.object(
            state_guard._FileLock,
            "_unlock",
            side_effect=OSError("unlock failed"),
        ):
            with self.assertRaises(state_guard.StateGuardError) as committed:
                self.apply({"facts": {"committed": True}})
        self.assertEqual(committed.exception.code, "LOCK_RELEASE_UNCERTAIN")
        self.assertTrue(state_guard.load_state(self.path)["facts"]["committed"])

        before = self.path.read_bytes()
        with mock.patch.object(
            state_guard._FileLock,
            "_unlock",
            side_effect=OSError("unlock failed"),
        ):
            with self.assertRaises(state_guard.StateGuardError) as original:
                self.apply({"facts": {"blocked": True}}, revision=2, actor="worker")
        self.assertEqual(original.exception.code, "OWNER_MISMATCH")
        self.assertEqual(self.path.read_bytes(), before)

    def test_26_malformed_schema_types_never_escape_raw_exceptions(self) -> None:
        mutations = {
            "schema_version": [],
            "task_id": [],
            "owner": {},
            "objective": 7,
            "objective_revision": [],
            "state_revision": {},
            "updated_at": [],
            "success_criteria": [],
            "facts": [],
            "pending": {},
        }
        for field, invalid_value in mutations.items():
            with self.subTest(field=field):
                candidate = dict(self.document)
                candidate[field] = invalid_value
                with self.assertRaises(state_guard.StateGuardError):
                    state_guard.validate_state(candidate)

        malformed_criteria = (
            {"tests": []},
            {"tests": {"statement": [], "status": "unknown"}},
            {"tests": {"statement": "x", "status": []}},
            {"tests": {"statement": "x", "status": "passed", "evidence_ref": []}},
        )
        for criteria in malformed_criteria:
            with self.subTest(criteria=criteria):
                candidate = dict(self.document)
                candidate["success_criteria"] = criteria
                with self.assertRaises(state_guard.StateGuardError):
                    state_guard.validate_state(candidate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
