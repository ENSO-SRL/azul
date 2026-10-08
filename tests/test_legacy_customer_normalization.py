"""Existing email-owned subscriptions need no manually inserted email alias."""
from datetime import datetime,timedelta,timezone
from unittest.mock import AsyncMock,MagicMock
import pytest
from test_subscription_identity import storage,add_subscription,EMAIL,UUID
from app.infrastructure.repo_impl import SQLRecurringRepository
from app.services.recurring_service import RecurringService

@pytest.mark.asyncio
@pytest.mark.parametrize('stored_customer',[EMAIL,EMAIL.upper(),' '+EMAIL+' ',UUID.upper(),' 228 '])
async def test_paid_legacy_identifiers_visible_from_canonical_id_without_alias_row(storage,stored_customer):
    db=storage();sub=add_subscription(db,stored_customer,token='fake',days=-40)
    charged=datetime.now(timezone.utc)-timedelta(days=1)
    sub.last_charged_at=charged;db.session.commit()
    gateway=MagicMock(sale_mit=AsyncMock())
    svc=RecurringService(MagicMock(),SQLRecurringRepository(db),MagicMock(),gateway,db_session=db)
    result=await svc.get_customer_status('228')
    assert result['allow_access'] is True
    assert result['reason']=='paid'
    assert result['total_subscriptions']==1
    assert result['valid_until']==(charged+timedelta(days=30)).isoformat()
    assert sub.customer_id==stored_customer
    gateway.sale_mit.assert_not_awaited()

@pytest.mark.asyncio
async def test_cardholder_email_is_not_account_ownership(storage):
    db=storage();sub=add_subscription(db,'somebody-else',token='fake',days=-40)
    sub.cardholder_email=EMAIL;sub.last_charged_at=datetime.now(timezone.utc)
    db.session.commit()
    svc=RecurringService(MagicMock(),SQLRecurringRepository(db),MagicMock(),MagicMock(),db_session=db)
    result=await svc.get_customer_status('228')
    assert result['allow_access'] is False
    assert result['reason']=='no_subscription'

@pytest.mark.asyncio
async def test_active_but_expired_is_not_paid_access(storage):
    db=storage();add_subscription(db,EMAIL,token='fake',days=-5)
    svc=RecurringService(MagicMock(),SQLRecurringRepository(db),MagicMock(),MagicMock(),db_session=db)
    result=await svc.get_customer_status('228')
    assert result['allow_access'] is False
    assert result['reason']=='payment_due'
