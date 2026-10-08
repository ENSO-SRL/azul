"""Frontend payment access is session-owned; server integrations use API auth."""
from fastapi import Request, Depends, HTTPException
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import SQLPaymentRepository
from app.security import require_api_key
from app.utils.token_utils import decode_user_info_token
from app.services.subscription_identity import resolve_customer_identity

def callback_signature(payment_id):
    import hashlib,hmac,os
    secret=os.getenv('API_KEY','')
    if not secret: raise RuntimeError('Callback signing unavailable')
    return hmac.new(secret.encode(),('atlas-3ds:'+payment_id).encode(),hashlib.sha256).hexdigest()

def verify_callback(payment_id,state):
    import hmac
    if not state or not hmac.compare_digest(callback_signature(payment_id),state):
        raise HTTPException(403,'Callback inválido.')

async def require_payment_access(request: Request, payment_id: str, db=Depends(get_db)):
    if request.headers.get('x-api-key'):
        await require_api_key(request.headers['x-api-key'])
        return
    user=(decode_user_info_token(request.cookies.get('user_info'),require_scope='user_info')
          or decode_user_info_token(request.cookies.get('access_token'),require_scope='access_token'))
    if not user: raise HTTPException(401,'Se requiere una sesión válida.')
    identity=await resolve_customer_identity(db,user.get('sub') or user.get('email',''))
    payment=await SQLPaymentRepository(db).get_by_id(payment_id)
    if payment is None or (payment.customer_id or '').strip().lower() not in identity.aliases:
        raise HTTPException(404,'Pago no encontrado.')
