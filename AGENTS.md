# Repository guidance

- This repository is the source of truth for website development. Keep changes reviewable in Git; use a feature branch for subsequent changes.
- Read `README.md` and `docs/development.md` before implementation. Keep the mainline and three-dimensional candidate pools independent.
- Never commit `.runtime/`, credentials, account databases, browser exports, source videos, or private deployment records. Do not overwrite deployed runtime databases during a code update.
- Preserve data provenance and timestamps. Missing or stale data must not be replaced by fabricated real-time values or silently substituted demo data.
- For Python behavior changes, run the relevant unittest modules; before a release, run `TDX_NO_PAPER_WORKER=1 python -m unittest discover -s tests` and the JavaScript tests in `tests/*.cjs`.
- A public GitHub push and a production deployment are separate actions. Deploy only within the user's authorized scope, preserving backups and runtime data.
