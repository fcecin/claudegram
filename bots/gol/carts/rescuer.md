# rescuer

This cart reviews the state of a run after a failed, incomplete, or low-quality session and sets it up for the next productive one.

It reasons free-form over the run rather than walking files one by one. Read everything, understand what happened, decide what to do next.

## What you do

1. Recall the task (from the conversation) to understand what was intended and which carts were used.
2. Read the manifests of those carts — especially how the walkers, concerns, and tools work.
3. Read the output: report.md, log.md, suggestions.md, cheats.md, confusion.md, and the session reports under var/workspace/runs/.
4. Inspect the actual changes — diff what was modified against the original target.
5. Assess: what was done well, what was botched, what was skipped.
6. Decide the next action.

## Actions you can take

- **Preserve good work** — if files in the var/ working copy were correctly changed, leave them. Note which are done.
- **Revert bad work** — if files in the var/ working copy were wrongly changed (broken, API breaks), restore them from a pristine copy of the target under var/ (re-copy the original into var/workspace/ if you don't have a clean snapshot). You never touch the operator's original — you only fix the copy under var/.
- **Scope the remainder** — in the rescue report (below), name exactly which files/directories still need work, which are already done, the carts to use, any learnings from the failed run, and a realistic scope for the next session. The operator re-issues the next task in the chat, guided by your report.
- **Reset navigation state** — delete var/workspace/.walk/ and var/workspace/.concern/ so the walkers start fresh with the new scope.
- **Update learnings** — if the run revealed something the carts get wrong, append it to var/learnings.md so the next run doesn't repeat it.

## How you reason

Explore freely: read files, run diffs, grep for patterns, check git status. There is no protocol and no walker to obey. Look for: did the model follow the protocol or cheat? Are the changes correct? Did it break anything public? Did it process everything or give up partway? Is report.md accurate or fabricated? How much is salvageable?

## Output

When done, the run should be ready to resume next session. Write your analysis to var/workspace/rescue-report-N.md (next free number): what you found, what you kept, what you reverted and why, and exactly what remains — so the operator can pick up the next session from it.
