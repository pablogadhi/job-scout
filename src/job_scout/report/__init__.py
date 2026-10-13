"""What the user reads: the daily digest, the backlog report, and the CSV/SQLite export.

`data.py` selects scored job groups and sorts them into sections (no I/O beyond the DB),
`render.py` turns them into HTML and plain text, `mailer.py` sends the digest over SMTP,
`export.py` writes the CSV and the SQLite snapshot.
"""
