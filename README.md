# SKILL.state Workflow for Codex and Claude Code

A cross-compatible Agent Skill for bounded, revisioned execution state, with a deterministic persistent JSON guard for recovery, handoff, compaction, waits, and parallel work.

The design is inspired by [SKILL.state: Scalable Long-Horizon Agent Skills](https://arxiv.org/abs/2608.26263). This repository is an independent implementation, not an official OpenAI or Anthropic project.

The root layout follows the shared `SKILL.md` Agent Skills format. Codex and Claude Code use the same instructions, references, guard, and tests; only their discovery metadata and always-on project instruction files differ.

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
- `docs/claude-code.md` — Claude Code installation and activation guide.
- `examples/global-CLAUDE-snippet.md` — first-turn activation rule for Claude Code.
- `tests/test_state_guard.py` — deterministic regression suite.
- `tests/test_claude_compat.py` — Claude Code layout and discovery checks.
- `EVALUATION.md` — routing, reliability, and context-scaling results.

## Install

### Codex

Clone the repository into the personal Codex skills directory:

```text
git clone https://github.com/Dakstar-cool/skill-state-workflow.git ~/.codex/skills/skill-state-workflow
```

If that destination already exists, preserve or move it before cloning. Do not overwrite personal changes blindly.

To activate the workflow from the first message of every project task, merge the rule in [`examples/global-AGENTS-snippet.md`](examples/global-AGENTS-snippet.md) into `~/.codex/AGENTS.md`.

### Claude Code

Clone the same repository into the personal Claude Code skills directory:

```text
git clone https://github.com/Dakstar-cool/skill-state-workflow.git ~/.claude/skills/skill-state-workflow
```

On Windows, `~/.claude` resolves to `%USERPROFILE%\.claude`. Merge [`examples/global-CLAUDE-snippet.md`](examples/global-CLAUDE-snippet.md) into `~/.claude/CLAUDE.md` to request the skill from the first turn of every project session. Invoke it directly with `/skill-state-workflow`, or let Claude select it from the frontmatter description.

Claude Code ignores the Codex-only `agents/openai.yaml`; it does not affect skill discovery. See the complete [Claude Code guide](docs/claude-code.md).

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

The current evaluation reports 52/52 workflow invariants, 26/26 guard regressions, 5/5 Claude compatibility checks, 22/22 unambiguous routing scenarios, and 10/10 adversarial two-writer races. Full methodology and limitations are in [`EVALUATION.md`](EVALUATION.md).
