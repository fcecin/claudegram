# code-processor

Processes a codebase file by file. The `walk` tool controls which file you work on.

You only ever modify files under var/. Before processing, copy the target into var/workspace/ and initialize the walk over the COPY — never the operator's original. Every file `walk next` returns is then under var/, and every edit stays under var/.

## Protocol

Follow this loop exactly. Do not deviate.

### 1. Initialize

Run `walk init` over the copy under var/workspace/, with any excludes the task calls for:

```
GOL/tools/walk init var/workspace/<target-copy> --exclude '*/vendor/*' --exclude '*/.git/*'
```

Default file pattern is `*` (all files); pass `--pattern '<glob>'` to narrow it. If a walk was already initialized in a previous session, the tool prints RESUMING and how many files remain. Do not pass `--force`; just proceed — `walk next` picks up where you left off. The file you were on when the previous session ended may be partially modified; re-read and re-process it fully.

### 2. Loop

Repeat until done:

```
file=$(GOL/tools/walk next)
```

If the output is `DONE`, stop — the run is complete. Otherwise:

- a. Append to var/workspace/log.md: `<timestamp> [code-processor] walk-enter <file>`
- b. Read the file `walk next` returned.
- c. Process it per the other loaded carts; if none is loaded, do the task's own processing here.
- d. If you made changes, write the modified file.
- e. Append `<timestamp> [code-processor] walk-exit <file>` to log.md, and append the file's one-line result (what changed, or `no change`) to var/workspace/report.md.
- f. `walk done` to advance.
- g. If the file needs no changes, `walk done` anyway.
- h. If the file should not be processed (generated code, etc.), `walk skip`.

### 3. Report

After `DONE`, run `walk status` and report the summary.

## Rules

- Never call `walk next` twice without `walk done` or `walk skip` between. The cursor does not advance until you tell it to.
- Never process a file `walk next` did not give you.
- One file per iteration. Never process multiple at once.
- On an error processing a file, `walk skip` it and note the error. Do not stop the walk.
- You may read other files for context, but you only MODIFY the file `walk next` gave you. Resolve and write all changes before `walk done`. If the current file reveals a problem elsewhere, note it in var/workspace/deferred.md — do not write to files you were not given.

## Index pass (optional)

Some tasks need cross-file context. Before the main loop you may run `walk index` (prints the full list), read each file, and build a summary into var/workspace/index.md. Then `walk reset` and run the main loop with the index available as context.
