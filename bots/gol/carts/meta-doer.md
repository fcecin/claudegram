# meta-doer

This cart takes the raw skills the doer built during a run and turns them into a polished, standalone cart under var/carts/ that any future task can depend on.

## Pre-flight

Read carts/doer.md first — its phases, how it builds skills, what formats it uses. You are promoting the materials that system produced, so you must understand it.

## Inputs

From a completed doer run you have:

- GOL/var/cache/doer/<skill>/ — the skill definitions the doer built
- var/workspace/study.md, skill-map.md, test-results.md, report.md — the evidence the skills work
- var/workspace/test/ — the test data
- var/learnings.md — anything learned

Read everything to understand what was built.

## What you do

1. Read the skills. Understand what each does, what it produced, how it works.
2. Read the evidence. Understand how the doer approached the task, what it found, what it tested, what worked.
3. Create the new cart at var/carts/<cart-name>.md — a proper manifest. (Produced carts go under var/carts/, not the read-only seed carts/. gol loads both, so a promoted cart is available exactly like a built-in.)
   - A clear description of what the cart does.
   - `depends:` derived from skill-map.md — INSTALLED steps name the carts used; BUILTIN (the file walker) → `depends: code-processor`; BUILD/CACHED steps become the cart's own content (instructions, tool files).
   - How the cart should be used; rules about what to fix vs. flag; API-break warnings if applicable.
4. Review and improve any tool files the doer produced (checks, scripts, verify commands). Are they correct, complete, well-formed? If the skill is prose-only (the doer applied it by hand, no scripts), keep the judgment parts as prose; where a step is deterministic, you may add a small tool plus a test for it. Put a produced cart's scripts in its own directory, var/carts/<cart-name>/.
5. Final review — examine the whole new cart with fresh eyes: dead files nothing references, duplicate or stale test data, regexes or commands that differ from what the doer actually used (compare against the cached skill and evidence), missing or broken references. Fix everything. If you change executable logic, write a regression test and run it — ensure it passes. The cart must be minimal and correct.
6. Update var/carts/index.md — add or replace this cart's one-line entry (`<cart-name> — <what it does>`, the same format as the kernel catalog); create the file if it does not exist. This is gol's catalog of the carts you have built.
7. Write a summary of what you built to var/workspace/promotion-report.md.

## Output

When done, var/carts/<cart-name>.md (plus any tools it needs) is a complete, working cart, and var/carts/index.md lists it with a one-liner.
