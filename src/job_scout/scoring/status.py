"""Facts for `job-scout status` and the digest footer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from job_scout.db.models import CallStatus, Run, ScoringCall
from job_scout.scoring.queue import pending_count

TOKEN_VALIDITY = timedelta(days=365)  # `claude setup-token` tokens last one year
TOKEN_WARN_BEFORE = timedelta(days=30)


def token_warning(created: date | None, today: date) -> str | None:
    """A warning when CLAUDE_CODE_OAUTH_TOKEN expires within 30 days (or has expired)."""
    if created is None:
        return "CLAUDE_CODE_OAUTH_TOKEN creation date unknown: set scoring.cli.token_created"
    expires = created + TOKEN_VALIDITY
    if today >= expires:
        return f"CLAUDE_CODE_OAUTH_TOKEN expired on {expires}: run `claude setup-token`"
    if expires - today <= TOKEN_WARN_BEFORE:
        return f"CLAUDE_CODE_OAUTH_TOKEN expires on {expires}: run `claude setup-token` soon"
    return None


@dataclass(frozen=True)
class ScoringStatus:
    pending: int
    batches_in_flight: int
    last_run: Run | None
    calls_today: int
    spend_today_usd: float  # api backend: estimated billed cost
    cli_cost_today_usd: float  # cli backend: API-equivalent estimate, not billed on Pro
    token_warning: str | None


def scoring_status(
    session: Session, profile_hash: str, timezone: str, token_created: date | None, now: datetime
) -> ScoringStatus:
    tz = ZoneInfo(timezone)
    local_midnight = datetime.combine(now.astimezone(tz).date(), time(), tzinfo=tz)
    today = ScoringCall.created_at >= local_midnight

    def cost(backend: str) -> float:
        return float(
            session.scalar(
                select(func.coalesce(func.sum(ScoringCall.cost_usd_estimate), 0.0)).where(
                    today, ScoringCall.backend == backend
                )
            )
            or 0.0
        )

    return ScoringStatus(
        pending=pending_count(session, profile_hash),
        batches_in_flight=session.scalar(
            select(func.count()).where(ScoringCall.status == CallStatus.SUBMITTED)
        )
        or 0,
        last_run=session.scalars(select(Run).order_by(Run.id.desc()).limit(1)).one_or_none(),
        calls_today=session.scalar(select(func.count()).where(today)) or 0,
        spend_today_usd=cost("api"),
        cli_cost_today_usd=cost("cli"),
        token_warning=token_warning(token_created, now.astimezone(tz).date()),
    )
