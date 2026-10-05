"""One entitlement decision for web, bot and payment summaries."""
from datetime import datetime, timedelta, timezone


def utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def access_decision(subscriptions, *, now=None):
    now = utc(now or datetime.now(timezone.utc))
    trials, paid = [], []
    active = []
    for sub in subscriptions:
        status = getattr(sub.status, 'value', sub.status)
        if status == 'ACTIVE':
            active.append(sub)
            end = utc(sub.trial_ends_at)
            if end and end > now:
                trials.append(end)
        charged = utc(sub.last_charged_at)
        if charged and sub.frequency_days > 0:
            end = charged + timedelta(days=sub.frequency_days)
            if end > now:
                paid.append(end)
    trial_end = max(trials) if trials else None
    paid_end = max(paid) if paid else None
    valid_until = max([v for v in (trial_end, paid_end) if v], default=None)
    allowed = valid_until is not None
    in_trial = bool(trial_end and (not paid_end or trial_end > paid_end))
    if allowed:
        reason = 'trial' if in_trial else 'paid'
    elif active and all(not s.data_vault_token for s in active):
        reason = 'trial_expired_no_card'
    elif active:
        reason = 'payment_due'
    else:
        reason = 'no_subscription'
    return {
        'allow_access': allowed, 'is_current': allowed, 'needs_payment': not allowed,
        'reason': reason, 'overall_status': reason,
        'valid_until': valid_until.isoformat() if valid_until else None,
        'paid_through': paid_end.isoformat() if paid_end else None,
        'in_trial': in_trial,
        'trial_ends_at': trial_end.isoformat() if trial_end else None,
        'requires_review': len(active) > 1,
    }
