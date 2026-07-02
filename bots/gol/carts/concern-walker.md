# concern-walker

Iterates over a list of concerns — abstract items like style rules, check categories, review topics. The `concern` tool works exactly like `walk`, but for concerns instead of files.

## Protocol

The cart or task that uses this provides a concerns file, pipe-separated:

```
<name> | <description> [<source>] | <verify command>
```

The third field (verify command) is optional. If present, `concern next <file>` runs it with `{file}` replaced by the file path and prints the output — you do not run verify commands manually, the tool does. Lines starting with `#` are comments; blank lines are ignored. `|` separates the name and description fields — do not use it there; the verify command is the last field and may contain pipes.

`{file}` is a single path. To check a concern across a whole directory in one verify, pass the directory as `<file>` and make the verify command recursive (`grep -rn …`), so one run covers every file under it.

A concern walk is **check-only** or a **fix**. Check-only: report findings, modify nothing — `interventions=0` and no `edit` lines, which is normal (not a deviation). Fix: change files (only copies under var/workspace/, never the target) and log each edit. The task says which; when it asks to report/find, or the target is read-only, it is check-only.

### Logging

Starting a concern, append to var/workspace/log.md:

```
<timestamp> [concern-walker] concern-start <concern text>
```

Finishing a concern:

```
<timestamp> [concern-walker] concern-end <concern text> interventions=<N>
```

Where `<N>` is the number of changes made for that concern on the current file (0 if none).

### Verification

When a concern can be checked mechanically ("no X", "no include"), use grep or other unix tools instead of guessing from memory. Read the concern text — if it says "do not use X", grep for X. Trust tool output over your own judgment.

### Tool-use logging

Every shell command you run while processing a concern MUST be logged. After running it, append:

```
<timestamp> [concern-walker] tool <concern-name> $ <full command line> output=<N>
```

Where `<N>` is the character count of stdout+stderr — this flags commands with unexpectedly large or empty output.

### File-modification logging

Every time you modify a file for a concern, immediately log it:

```
<timestamp> [concern-walker] edit <concern-name> <file> <changelog>
```

The changelog must let a reviewer trace the change without the diff: what changed, where (line or symbol), and how. If you made no changes for a concern, write no edit line.

### Resuming

When `concern init` prints RESUMING, a previous session was interrupted mid-file. Do not reset — call `concern next` and continue from the current concern. Some concerns may already be applied to the partial file; that's fine.

## Rules

- Never call `concern next` twice without `concern done` or `concern skip`.
- One concern at a time. Do not batch.
- The concern text is your instruction for what to check. Read it carefully each time — do not rely on memory of previous concerns.
