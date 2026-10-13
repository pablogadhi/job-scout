"""Opt-in: send one real test email through the configured SMTP server.

    uv run --env-file .env pytest -m live tests/test_live_email.py

Needs JOB_SCOUT_SMTP_TOKEN and digest.email.from_address / to_address (settings.yaml or
JOB_SCOUT_DIGEST__EMAIL__FROM_ADDRESS / __TO_ADDRESS). Sends exactly one message.
"""

import os
from datetime import UTC, datetime

import pytest

from job_scout.config import load_config
from job_scout.report import mailer
from tests.conftest import REPO_CONFIG

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.environ.get(mailer.TOKEN_ENV), reason=f"{mailer.TOKEN_ENV} not set")
def test_send_one_email() -> None:
    email = load_config(REPO_CONFIG).settings.digest.email
    if not (email.from_address and email.to_address):
        pytest.skip("digest.email.from_address / to_address not set")
    now = datetime.now(UTC)
    message = mailer.build_message(
        email,
        "job-scout test email",
        f"Test email from job-scout at {now:%Y-%m-%d %H:%M} UTC. Nothing to do.",
        f"<p>Test email from job-scout at {now:%Y-%m-%d %H:%M} UTC. Nothing to do.</p>",
        now,
    )
    mailer.send(email, message, os.environ)
