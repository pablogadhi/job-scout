"""Opt-in: send one real test email through the configured SMTP server.

    uv run --env-file .env pytest -m live tests/test_live_email.py

Needs JOB_SCOUT_SMTP_TOKEN, digest.email.from_address (settings.yaml) and the recipient in
JOB_SCOUT_DIGEST__EMAIL__TO_ADDRESS. Sends exactly one message.
"""

import os
from datetime import UTC, datetime

import pytest

from job_scout.config import load_config
from job_scout.report import mailer
from tests.conftest import REPO_CONFIG

pytestmark = pytest.mark.live

# Read at import: conftest clears JOB_SCOUT_* settings before each test runs.
TO_ADDRESS = os.environ.get(mailer.TO_ENV)


@pytest.mark.skipif(not os.environ.get(mailer.TOKEN_ENV), reason=f"{mailer.TOKEN_ENV} not set")
def test_send_one_email() -> None:
    email = load_config(REPO_CONFIG).settings.digest.email
    email = email.model_copy(update={"to_address": TO_ADDRESS or email.to_address})
    if not (email.from_address and email.recipients):
        pytest.skip(f"digest.email.from_address or {mailer.TO_ENV} not set")
    now = datetime.now(UTC)
    message = mailer.build_message(
        email,
        "job-scout test email",
        f"Test email from job-scout at {now:%Y-%m-%d %H:%M} UTC. Nothing to do.",
        f"<p>Test email from job-scout at {now:%Y-%m-%d %H:%M} UTC. Nothing to do.</p>",
        now,
    )
    mailer.send(email, message, mailer.read_token(os.environ))
