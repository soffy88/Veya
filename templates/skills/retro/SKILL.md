---
name: retro
description: Write a short post-task lesson (mistake → next-time rule) into Genesis experience log and user memory. Use at the end of a failed or surprising task, or when the user asks to retrospect.
---

# retro

Call this when a task finished with a real miss, a user correction, or an explicit "let's retro". Do not invent lessons.

## record

`action=record` with `mistake` and `lesson`. Optional `context`. Writes to Genesis `experiences.jsonl` and the user's memory store (`kind=summary`).

## recent

`action=recent` to recall the last lessons before repeating a known failure.

This is not a second memory system and not a slash command.
