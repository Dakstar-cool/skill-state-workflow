# SKILL.state Workflow for Codex

A personal Codex skill for bounded, revisioned execution state, with a deterministic persistent JSON guard for recovery, handoff, compaction, waits, and parallel work.

The design is inspired by [SKILL.state: Scalable Long-Horizon Agent Skills](https://arxiv.org/abs/2608.26263). This repository is an independent implementation, not an official OpenAI project.

## Behavior

The skill uses progressive disclosure:

| Level | Loaded when | State form |
|---|---|---|
| L1 | Every project task; explicit or resumable Projectless work | Compact in-context state |
| L2 | Wait, retry/recovery, uncertain effect, handoff, compaction, or parallel canonical facts | Long-horizon rules |
| L3 | State must survive a durable boundary | Guarded persistent JSON |

Ordinary one-shot Projectless chats remain outside the workflow.

## Contents

- `SKILL.md` — compact activation and state rules.
- `references/long-horizon.md` — recovery, boundedness, evidence, and sole-writer rules.
- `references/persistent-state.md` — persistent schema and CLI contract.
- `scripts/state_guard.py` — standard-library-only JSON guard.
- `agents/openai.yaml` — Codex UI and implicit-invocation metadata.
- `tests/test_state_guard.py` — deterministic regression suite.
- `EVALUATION.md` — routing, reliability, and context-scaling results.

## Install

Clone the repository into the personal Codex skills directory:

```text
git clone https://github.com/Dakstar-cool/skill-state-workflow.git ~/.codex/skills/skill-state-workflow
```

If that destination already exists, preserve or move it before cloning. Do not overwrite personal changes blindly.

To activate the workflow from the first message of every project task, merge the rule in [`examples/global-AGENTS-snippet.md`](examples/global-AGENTS-snippet.md) into `~/.codex/AGENTS.md`.

## Persistent guard

The guard provides:

- strict UTF-8 JSON and exact schema checks;
- bounded canonical state (32 KiB by default);
- compare-and-swap through `state_revision`;
- one logical owner and atomic handoff;
- a cross-process sidecar lock;
- candidate-first validation and no-op detection;
- same-directory temp write, `fsync`, and atomic replace;
- current-objective evidence enforcement;
- trusted-root and symlink/reparse/hard-link defenses.

See all commands:

```text
python scripts/state_guard.py --help
```

The guard protects the state-file transaction only. External API and filesystem effects require read-only reconciliation when their outcome is uncertain.

## Test

No third-party Python packages are required:

```text
python -m unittest discover -s tests -v
```

The suite covers schema failures, byte invariance, CAS races, cross-process locking, evidence revisions, owner handoff, strict JSON, path checks, and atomic-write failure modes.

## Scope limits

The persistent layer assumes cooperating writers and a trusted local filesystem. It does not claim hostile-process isolation, network-filesystem locking guarantees, or Windows power-loss directory durability. See [`references/persistent-state.md`](references/persistent-state.md) for the full boundary.

## Evaluation

The current evaluation reports 52/52 workflow invariants, 26/26 guard regressions, 22/22 unambiguous routing scenarios, and 10/10 adversarial two-writer races. Full methodology and limitations are in [`EVALUATION.md`](EVALUATION.md).
