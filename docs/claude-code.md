# Claude Code compatibility

Claude Code and Codex can use the same skill core because both discover a directory containing `SKILL.md` with YAML frontmatter, Markdown instructions, optional references, and executable scripts.

## Personal installation

Personal skills are available across Claude Code projects:

```text
git clone https://github.com/Dakstar-cool/skill-state-workflow.git ~/.claude/skills/skill-state-workflow
```

Windows resolves this location to `%USERPROFILE%\.claude\skills\skill-state-workflow`. If the destination exists, preserve local changes before updating it.

## Project installation

To share the skill in one repository, place it at:

```text
<project>/.claude/skills/skill-state-workflow/SKILL.md
```

Keep `references/` and `scripts/` beside `SKILL.md`. Project skills can be committed and shared with the team.

## Invocation

Claude may select the skill automatically from its `description`, or the user may invoke it directly:

```text
/skill-state-workflow
```

For first-turn activation in every project, merge [`../examples/global-CLAUDE-snippet.md`](../examples/global-CLAUDE-snippet.md) into `~/.claude/CLAUDE.md`. A project can instead keep the same rule in `./CLAUDE.md` or `./.claude/CLAUDE.md`.

`CLAUDE.md` is persistent context, not a hard enforcement boundary. Permissions and hooks remain separate Claude Code configuration.

## Bundled guard

Claude Code exposes `${CLAUDE_SKILL_DIR}` while rendering an invoked skill. Use that resolved directory for the guard:

```text
<python> ${CLAUDE_SKILL_DIR}/scripts/state_guard.py --help
```

The guard itself uses only the Python standard library and is identical for Codex and Claude Code. The Codex-specific `agents/openai.yaml` file is ignored by Claude Code.

## Verification boundary

The repository checks the documented Claude Code filesystem layout, frontmatter, supporting-resource links, skill-directory variable, and global activation snippet. Guard behavior is tested on Windows and Linux. A local Claude Code executable was not available during this compatibility pass, so this is format and runtime verification rather than an interactive model-behavior A/B test.

Official references:

- [Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [How Claude remembers your project](https://code.claude.com/docs/en/memory)
