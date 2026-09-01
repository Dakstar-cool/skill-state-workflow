# Long-Horizon Execution

Use structured state as the canonical representation of current execution. Do not depend on conversation history to reconstruct what is currently true.

## State schema

Initialize the compact core and only optional fields that future decisions need:

```yaml
objective: current requested outcome
objective_revision: positive integer starting at 1
success_criteria: criterion_id -> statement, status, evidence_ref
facts: verified current facts needed for future decisions
pending: ordered unresolved actions or checks
last_observation: newest relevant tool or environment result
# Optional: phase, decisions, artifacts, risks, retry_counts
```

Keep state internal when sufficient. Persist a compact artifact only for recovery across compaction, restart, handoff, or an extended wait, following the workspace's scratch-file convention. Recovery needs override the no-file default for otherwise simple tasks.

An extended wait is one likely to outlive the active turn or usable model context, including scheduled continuation. Risky recovery means an uncertain external effect or recovery across restart, handoff, or compaction; persist before crossing that boundary.

Before creating or mutating that artifact, read [persistent-state.md](persistent-state.md) completely and use its deterministic guard. This extra layer applies only to persistent state; do not load it for ordinary in-context updates.

## Authority and revisions

After an objective revision, replace superseded criteria and prune invalid pending work and facts. Preserve artifacts and evidence that still apply to the new revision.

## State transitions

After each meaningful action, validate the newest observation, update only affected fields, preserve unrelated state, then select the next action. An invalid or inconsistent proposed update leaves the previous state unchanged.

For volatile or conflicting facts, compare source authority, scope, and observation time. A newer observation supersedes an older fact only within the same scope and when its source is sufficiently authoritative; otherwise mark uncertainty and reconcile it read-only.

## Boundedness

Normalize repeated facts into keyed current summaries; prune superseded facts; replace growing lists with counters, indexes, or bounded recent windows. Keep raw evidence outside working state and reference it only when needed.

If compact state would lose information needed for recovery or verification, retain that source material outside state and reference it.

## Failure and recovery

When an action fails before taking effect, roll back any provisional delta. If an external side effect may have occurred but its outcome is uncertain, mark it uncertain and reconcile read-only before retrying. Never assume remote rollback or idempotency.

A failure is repeated when the same operation or failure class occurs twice, or a retry/recovery loop starts. Track it and follow applicable workspace retry and research rules.

## Parallel work and handoff

One owner is the sole canonical-state writer. Workers return bounded verified observations or proposed deltas without gaining ownership; the owner validates and merges them. On handoff, transfer the objective revision, state, pending work, risks, and evidence references before the recipient becomes owner.

## Completion

For each nontrivial criterion, keep `status: unknown | passed | failed` and an `evidence_ref` scoped to the current objective revision and relevant branch, commit, artifact, or external object when applicable.

Declare completion only when every required criterion is `passed` against current evidence.
