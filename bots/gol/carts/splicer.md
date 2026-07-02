# splicer

depends: code-processor

Splits a large file-processing task into scopes (directory segments). Each session processes one scope. The splicer tracks which scopes are done so later sessions pick up at the next scope automatically.

## Protocol

The cart or task that uses this provides a splice plan — a text file with one directory path per line, each a scope. Copy the target into var/workspace/ first, then generate the plan over the COPY: `GOL/tools/splice-plan var/workspace/<target-copy>` redirected to var/workspace/splice-plan.txt. Every scope is then a directory under var/, so all walking and editing stays under var/.

### Startup

Run `splice init <plan-file>`. If resuming, it prints RESUMING with the current scope. Then `splice current` for the active scope. If it prints `ALL SCOPES DONE`, the whole task is finished — write a session report and stop; do not process anything. Print a clear banner:

```
=== ALL SCOPES COMPLETE ===
No more work. Final results are in var/workspace/report.md
```

### Per-scope

1. `splice current` — the directory to process.
2. `walk init <scope-dir> --maxdepth 1 --force` (add `--pattern '<glob>'` when the task targets specific file types) — `--maxdepth 1` so the walker only takes files directly in this scope (subdirectories are their own scopes); `--force` for a fresh walker per scope.
3. Re-initialize any other iterators (e.g. concern) with `--force` too. Each scope starts clean.
4. Process all files in the scope via the walker protocol.
5. When `walk next` prints DONE, the scope is finished.
6. `splice done` — advance to the next scope.
7. Stop the session. A later session picks up at the next scope.

### One scope per session

Process ONE scope per session, then stop. The cursor persists between sessions, so nothing is repeated.

### Output across sessions

var/workspace/report.md and var/workspace/log.md are append-only — they accumulate across scopes and sessions. Never overwrite; always append. Each session writes its own numbered session report under var/workspace/runs/. If a session ends mid-scope, the walker cursor preserves progress; the next session resumes both the splice (same scope) and the walk (same file).
