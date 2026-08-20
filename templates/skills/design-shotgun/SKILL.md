---
name: design-shotgun
description: Multi-option UX/design comparison board and design-to-code checklist. Does not route the main agent — fill the board yourself with vision_* / hicode_run.
---

# design-shotgun

Use when the user wants **several UI/UX approaches compared**, a design review, or a mockup/HTML turned into product code.

This skill is a **scaffold**. It does not call a second model and does not decide that a task is "design". You (the main agent) still choose tools.

## shotgun

Call `run_skill` / this package with `action=shotgun` and `brief`. Fill 3–5 options on the returned axes (`user_job`, `complexity`, `risk`, `aesthetics`, `impl_cost`). If images exist, `vision_glance` / `vision_ground` first. Do **not** pick a winner until the user asks.

## pick

After the board is filled, `action=pick` with `options_json`. Name the winner and why the others were dropped. No silent ranking in prose without this step when the user asked for a comparison.

## html_to_code

`action=html_to_code`. Follow the returned steps: screenshot → vision → implement with `hicode_run` in the user workspace. Do not rewrite the design in chat instead of code.
