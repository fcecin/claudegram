# gol

gol is a context scheduler. It works through tasks one step at a time, with its capabilities loaded as cartridges and its work recorded under var/.

On boot, and again after any context compaction, read:

1. kernel.md — the firmware; it includes a one-line catalog of the seed carts.
2. var/carts/index.md — the one-line catalog of carts you have built, if it exists.
3. var/learnings.md — if it exists.

Do NOT read the carts on boot. When a task arrives, pick the cart(s) it needs from the kernel's catalog and read only those manifests in full (plus any they depend on). Re-read kernel.md and any mounted manifests from disk, not memory, before starting work and whenever the rules are unclear.
