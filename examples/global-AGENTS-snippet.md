# Global AGENTS.md snippet

Merge the following rule into `~/.codex/AGENTS.md` when the skill should start automatically for every project task:

```md
## SKILL.state for project tasks

If the current task has project context, including a saved project, repository, or project workspace, load and apply `$skill-state-workflow` before any work from the first message and continue using it until the task is complete.

In a Projectless chat, do not activate `$skill-state-workflow` automatically for an ordinary one-shot task. Activate it when explicitly requested, when unfinished state is expected to survive the current turn or session, or on the first turn where recovery/retry, waiting, compaction, handoff, or parallel canonical-state coordination appears.
```

Keep any stricter workspace-specific safety and retry rules alongside this snippet.
