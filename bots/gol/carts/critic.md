# critic

This cart assesses the quality of a completed run against its success criteria and reports problems. It does not fix anything and does not suggest improvements; it writes a critique.

## Pre-flight

If var/workspace/critique.md already exists, print `critique.md already exists. Nothing to do.` and stop. Do not overwrite, do not append. One critique per run.

## What you do

1. Recall the task (from the conversation) to understand the original intent.
2. Read every cart manifest that was used, following the dependency chain.
3. Read var/workspace/report.md — the claimed results.
4. Read var/workspace/log.md — the actual activity.
5. Read var/workspace/suggestions.md, cheats.md, confusion.md if present.
6. Read the session reports under var/workspace/runs/ — the model's own assessment.
7. Inspect the actual changes: diff what was modified against the original target.
8. Read var/learnings.md if it exists.

## How you critique

Assess each dimension below and report problems with specific evidence.

- **Protocol adherence** — Were the cart protocols followed exactly? Did it cheat, and how badly — were the cheats justified? Did it log everything? Are the timestamps real or batched? Did it skip or batch work it should have done one at a time? Did it write reports for every unit?
- **Change quality** — Are the changes correct against the task's success criteria? Did it break anything public? Did it miss obvious violations the verify commands should have caught? Did it apply changes it should have flagged as suggestions? Did it confuse similar-but-different rules? Would the changes work?
- **Judgment** — On judgment calls, were they good? Did it err toward caution (good) or recklessness (bad)? Did it understand context or just pattern-match?
- **Completeness** — How much of the intended work was actually done? Were any files or scopes silently skipped? Is report.md accurate or fabricated?
- **Efficiency** — Wasted budget? Unnecessary tool calls? Files read that weren't needed?

## Output

Your only new file is var/workspace/critique.md (log your own actions to log.md as usual). Do not modify or overwrite the graded run's report.md, produced files, or other artifacts — you assess, you do not fix.

Write var/workspace/critique.md:

```markdown
# Critique

## Rating: X/5 stars
[one-sentence summary]

## Protocol adherence
## Change quality
[specific examples — file names, lines, exact changes]
## Judgment calls
## Completeness
## Efficiency
## Specific issues
[numbered, worst to least]
## What was done well
[be fair — acknowledge good work, don't inflate it]
```

## Rating scale

- 5.0 — exceeds a human expert; flawless. Almost never given.
- 4.5 — near-expert, trivial issues only.
- 4.0 — equal to a careful human expert; minor inefficiencies at most.
- 3.5 — good, a few easily-caught mistakes.
- 3.0 — decent bot work; mostly correct, needs some human review.
- 2.5 — mixed; some good, some bad.
- 2.0 — OK, saves effort but needs significant review.
- 1.5 — below expectations; more wrong than right.
- 1.0 — mediocre; cost probably not justified.
- 0.5 — almost nothing of value.
- 0.0 — total garbage; worse than doing nothing; may have introduced bugs.

Be precise; 3.5 means something different from 3.0. Justify the rating with evidence.
