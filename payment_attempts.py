"""Pure selectors for a checkout's current review; not dispatch/closure proof."""
from collections.abc import Mapping
from datetime import datetime, timezone


def current_review(pending):
    child = pending.get('recovery')
    return child if isinstance(child, Mapping) else pending


def review_method(pending, attempt):
    choice = ((attempt.get('browser_review') or {}).get('payment_choice') or {}) if attempt is not pending else (pending.get('checkout_payment') or {})
    return choice.get('method')


def _confirmation_deadline(attempt):
    try:
        expiry = datetime.fromisoformat(attempt.get('expires_at'))
    except (ValueError, TypeError):
        return None
    return expiry if expiry.tzinfo is not None else None


def confirmation_deadline_expired(attempt, instant):
    """Authorization requires a known, future deadline."""
    expiry = _confirmation_deadline(attempt)
    return expiry is None or instant >= expiry


def can_discard_expired_review(attempt, instant):
    """Automatic cleanup needs positive evidence that an undispatched review elapsed."""
    if not isinstance(attempt, Mapping) or attempt.get('status') != 'awaiting_confirmation':
        return False
    expiry = _confirmation_deadline(attempt)
    return expiry is not None and instant >= expiry


def review_expired(attempt, now=None):
    return (isinstance(attempt, Mapping) and attempt.get('status') == 'awaiting_confirmation'
            and confirmation_deadline_expired(attempt, now or datetime.now(timezone.utc)))
