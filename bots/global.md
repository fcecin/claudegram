# Shared note for every bot — your working directory

You're running on someone else's computer, so please keep your files tidy and in
one place instead of scattering them across the system (e.g. don't write into
`/tmp`).

- Your workspace is the **`work/`** directory of your install — it's also your
  default working directory (cwd).
- Make yourself a **subfolder named after you** and work inside it:
  `work/<your-bot-name>/` — for example `work/ava/` or `work/claude/`, depending
  on which bot you are (your name is given to you at the top of every turn).
- If you need a scratch/temp area, create one **inside your own subfolder**
  (e.g. `work/<your-bot-name>/tmp/`) rather than using the system `/tmp`.
- Inside your own subfolder you can **create, edit, and delete files and
  directories freely** — that space is yours.

## Staying current (never automatic — only with the user's OK)

If the machine has **git** and your install is a clone, you may occasionally — when
idle, e.g. on a periodic tick and it's been a while — check whether CloudGram's
upstream has moved. If it has, **do not update yourself**: tell the user and ask,
e.g. "CloudGram has a newer version — want me to `git pull` and restart to update?"
Update **only on their explicit approval**; if they decline (or have said not to),
leave it.
