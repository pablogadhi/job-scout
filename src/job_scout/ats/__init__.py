"""Company job boards (ATS): the remoteintech seed, board detection, and polling.

- `seed.py`: parse the seed tarball (one Markdown file per company) and filter it.
- `platforms.py`: per-ATS token patterns, payload readers and posting mappers.
- `detect.py`: find a company's board (careers URL, careers page, slug probing).
- `store.py`: the `ats_companies` table (seed upserts, watchlist, what to detect or poll).
- `refresh.py`: download the seed, apply the watchlist, detect; used by `ats detect` and `run`.
- `fetcher.py`: the `remoteintech` source's fetcher, polling every known board.

Network access goes through `fetch.http.HttpClient` only.
"""
