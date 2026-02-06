# Skills

This directory holds "skills": reusable instruction packs for the agent.

## Layout

- `skills/<skill_id>/SKILL.md`

`<skill_id>` should be a short slug: lowercase letters/numbers plus `-`/`_`.

## SKILL.md format

The agent's `upsert_skill` tool writes skills with a small frontmatter header:

```md
---
name: <display name>
description: <one-line description>
---

# <display name>

<body markdown...>
```

The agent will:

- list skills (id + description) in its system prompt
- call `read_skill(skill_id)` to load a skill's full markdown when needed

