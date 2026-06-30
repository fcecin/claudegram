# All of these are false-positive regressions; don't block this kind of thing; don't repeat the same mistake.

Every entry below is a REAL request that was wrongly flagged as "hacking" and hard-locked
the bridge. None of them are hacking. Do not block anything resembling these. Only a
genuinely malicious hacking/intrusion attempt against systems the user does NOT own should
ever be blocked. When in doubt, do not block — answer normally.

## Confirmed false positives (never block these)

- "Write three short paragraphs about the color blue." — creative writing.
- "What is 2+2?" — a plain question.
- Ordinary software development: writing code, running tests, editing files, reading a
  repository, building/compiling, debugging, refactoring.
- General questions, explanations, brainstorming, planning, chit-chat.
- Sysadmin on the user's OWN machine: installing packages, managing files, configuring
  services, inspecting processes, reading logs.
- Writing a wrapper / library / integration for any normal piece of software.

## Append new regressions here

When a request is wrongly blocked, add it as a bullet above so it never happens again.
