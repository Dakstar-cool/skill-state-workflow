# Persistent State Guard

Use the guard only after `long-horizon.md` determines that state must survive a compaction, restart, handoff, extended wait, or risky recovery. Ordinary project work keeps the compact core internal and does not load or run this layer.

Read this file completely before creating or mutating a persistent state file.

## Boundary and guarantees

Keep the state file in a trusted local workspace scratch directory. The CLI confines paths to `--root` (the current directory by default) and rejects symlinks and Windows reparse points. Its lock/rename assumptions do not extend to SMB, NFS, FUSE, hostile same-account processes, or non-cooperating writers.

The guard provides strict schema validation, a single logical owner, compare-and-swap by `state_revision`, a persistent sidecar lock, bounded canonical JSON, and same-directory atomic replacement. `state_revision`, never `updated_at`, is the ordering authority. On Windows, standard-library Python cannot promise directory-flush durability across sudden power loss.

The owner field prevents accidental concurrent writes; it is not authentication. External actions are never part of the file transaction. If an external effect may have occurred, reconcile it read-only before another attempt and record only the verified result.

## Canonical document

The flat JSON envelope has these required fields:

```json
{
  "schema_version": 1,
  "task_id": "stable-task-id",
  "owner": "canonical-writer-id",
  "objective": "current requested outcome",
  "objective_revision": 1,
  "state_revision": 1,
  "updated_at": "2026-09-01T12:00:00Z",
  "success_criteria": {
    "criterion-id": {
      "statement": "observable condition",
      "status": "unknown"
    }
  },
  "facts": {},
  "pending": []
}
```

Optional top-level fields are `last_observation`, `phase`, `decisions`, `artifacts`, `risks`, `uncertainties`, and `retry_counts`. Put domain-specific values inside `facts`, not in new top-level fields.

Statuses are `unknown`, `passed`, or `failed`. A passed criterion requires:

```json
"evidence_ref": {
  "ref": "test or artifact locator",
  "objective_revision": 1,
  "scope": "branch, commit, artifact, or external object"
}
```

`source` and `observed_at` are optional evidence fields. Evidence from another objective revision is rejected.

## Commands

Use the bundled Python runtime when the system Python is missing. In these examples, `<python>` is the chosen interpreter and `<skill>` is this skill directory. Claude Code resolves `<skill>` through `${CLAUDE_SKILL_DIR}` from the invoked `SKILL.md`; Codex resolves it from the installed skill location.

Create the scratch directory first (`New-Item -ItemType Directory -Force .codex-state` in PowerShell or `install -d -m 700 .codex-state` on POSIX). The guard refuses missing parent directories instead of guessing where state belongs. State, lock, and temporary files are restricted by the guard; protect user-created patch files with the same workspace access policy.

Initialize once; `init` never overwrites an existing state:

```text
<python> <skill>/scripts/state_guard.py init .codex-state/task.json --task-id task-42 --owner root --objective "Ship the verified change" --criterion tests="Required tests pass" --criterion review="Diff is reviewed"
```

Validate before recovery or resumption:

```text
<python> <skill>/scripts/state_guard.py check .codex-state/task.json --task-id task-42 --owner root
```

Apply a patch only with the revision returned by the last successful operation:

```text
<python> <skill>/scripts/state_guard.py apply .codex-state/task.json .codex-state/patch.json --task-id task-42 --owner root --expected-revision 3
```

Transfer sole-writer ownership atomically:

```text
<python> <skill>/scripts/state_guard.py handoff .codex-state/task.json --task-id task-42 --owner root --new-owner reviewer --expected-revision 4
```

Pass `--root <trusted-workspace>` when the current directory is not the intended boundary. Success and failure are single-line JSON metadata; state contents are not printed.

## Patch contract

The patch root is an object. Maps merge recursively, lists and scalars replace the entire leaf, and JSON `null` deletes an object member. Deleting an absent member is a no-op. An object patch aimed at an existing scalar or list is rejected as a type conflict; delete that leaf in one verified revision before adding the object in the next. Because `null` is the deletion sentinel, persistent null values are not supported.

The patch cannot write `schema_version`, `task_id`, `owner`, `objective_revision`, `state_revision`, or `updated_at`. Use `handoff` for owner changes. Use `--new-objective-revision N` for an objective, criterion ID/statement, or load-bearing constraint change; `N` must be exactly the current objective revision plus one. Reconcile or reset stale criterion evidence in the same patch.

The guard merges into a copy, validates the full candidate, and writes only after every check passes. A rejected change leaves the state file byte-for-byte unchanged. A no-op does not rewrite the file, change its timestamp, or increment `state_revision`; every actual mutation increments it exactly once.

## Recovery sequence

1. Run `check` against the expected task and owner.
2. If JSON or schema validation fails, preserve the file for diagnosis; never auto-reinitialize it.
3. If the revision is stale, reload, compare scopes, and rebuild the patch from current facts.
4. If the owner differs, obtain an explicit handoff; do not impersonate it.
5. If a write outcome is reported uncertain, read and reconcile before retrying.
6. Keep raw logs and large evidence outside state; store bounded references only.

The permanent `<state>.lock` file is intentional. Do not delete it during normal operation.
