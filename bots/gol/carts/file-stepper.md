# file-stepper

depends: code-processor

Processes one file per session. The walker iterates as usual, but this cart enforces a hard stop after each file. A later session resumes at the next file. Use it for tasks where each file consumes significant context (e.g. many concerns per file) — one file per session keeps the context window fresh and prevents degradation.

## Protocol

### Startup

Initialize the walker as needed (it RESUMES if already initialized), then `walk next` for the current file. If it prints `DONE`, all files are processed — print:

```
=== ALL FILES COMPLETE ===
Final results are in var/workspace/report.md
```

Write the session report and stop.

### Per-file

1. `walk next` — the file to process.
2. Process it per the other loaded carts.
3. `walk done`.
4. Write the session report and stop. ONE file per session. Do not call `walk next` again after `walk done` — the session is over. A later session picks up the next file automatically.

### Output across sessions

var/workspace/report.md and var/workspace/log.md are append-only — always append. Each session writes its own numbered report under var/workspace/runs/.
