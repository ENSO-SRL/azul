from routers.recurring import CustomerStatusResponse
from app.services.access_policy import access_decision


def test_customer_status_response_preserves_access_contract():
    payload = {
        'customer_id': '141', 'has_subscriptions': False, 'is_active': False,
        'has_overdue_payment': False, 'total_subscriptions': 0,
        'active_count': 0, 'paused_count': 0, 'cancelled_count': 0,
        'subscriptions': [], **access_decision([]),
    }
    serialized = CustomerStatusResponse(**payload).model_dump()
    for field in ('allow_access', 'needs_payment', 'valid_until', 'paid_through',
                  'reason', 'overall_status', 'requires_review'):
        assert field in serialized
        assert serialized[field] == payload[field]
