"""Pure selectors for a checkout's current review; not dispatch/closure proof."""
from collections.abc import Mapping
from datetime import datetime, timezone


def current_review(pending):
    child = pending.get('recovery')
    return child if isinstance(child, Mapping) else pending


def review_method(pending, attempt):
    choice = ((attempt.get('browser_review') or {}).get('payment_choice') or {}) if attempt is not pending else (pending.get('checkout_payment') or {})
    return choice.get('method')


def review_expired(attempt, now=None):
    if attempt.get('status') != 'awaiting_confirmation':
        return False
    try:
        expiry = datetime.fromisoformat(attempt.get('expires_at', ''))
        return expiry.tzinfo is not None and expiry <= (now or datetime.now(timezone.utc))
    except (ValueError, TypeError):
        return False
