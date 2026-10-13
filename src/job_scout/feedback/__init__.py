"""Thumbs up/down on digest items, and your judgments fed back into scoring (PLAN.md Phase 8).

- `links.py`: signed per-job links for the digest (secret from JOB_SCOUT_FEEDBACK_SECRET).
- `store.py`: one `feedback` row per canonical job, with the score you reacted to.
- `examples.py`: picks recent judgments for the scoring prompt (`scoring.prompt`).
- `server.py`: the small page the digest links to (`job-scout feedback serve`).
- `report.py`: how often Claude's scores agreed with you (`job-scout feedback report`).
"""
