# Counter: agent rules

Hackathon entry (ArmorIQ "Flow State", due 2026-10-17). Read in order: `SPEC.md`, `CONTRACTS.md`, `PROGRESS.md`.

- Work only in the directories your task names. Other parts are built in parallel on other branches.
- Secrets: `~/.config/counter/.env`. Never print, log, commit or echo a secret value or the owner email. Don't `cat` that file; load it with python-dotenv.
- ArmorIQ is live (org probe-org, policy `counter-policy`, variant B). Don't change policies, keys or members. Reject any delegation your tests leave pending (`reference/armoriq/tests_lib.py reject()`).
- Python: `uv`, Python 3.12, `armoriq-sdk==0.6.10`, `fastmcp`, `fastapi`, `uvicorn`, `google-genai`, `slack_bolt`. Tests with `pytest`.
- "Done" means you ran it: tests pass and the live checks in your task print the expected results. Paste the real output into `PROGRESS.md` under your section. Don't claim anything you didn't run.
- Commit to your branch with clear messages when a piece works. Don't push, don't open PRs.
- Plain writing in docs and UI copy: short sentences, no hype.
