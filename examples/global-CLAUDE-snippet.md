# Global CLAUDE.md snippet

Merge the following rule into `~/.claude/CLAUDE.md` when the skill should start from the first turn of every Claude Code project session:

```md
## SKILL.state for project tasks

At the beginning of every project session, load and apply the `skill-state-workflow` skill before other work. Continue using its bounded objective, criteria, evidence, facts, and pending state until the task is complete. After compaction, make sure the skill remains active and reload it if necessary.

Outside project work, skip the skill for an ordinary one-shot request. Activate it when explicitly requested, when unfinished state must survive the current turn or session, or when recovery/retry, waiting, compaction, handoff, or parallel canonical-state coordination appears.
```

Claude Code treats `CLAUDE.md` as persistent session context rather than enforced configuration. Keep this rule concise and preserve any stricter safety or permission rules already present.
