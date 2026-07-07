"""
Tunnel — Secure Payment Link (No DB storage).

GET  /tunnel          → Formulario HTML de pago (requiere ?ref= con resumen en Redis)
POST /tunnel/process  → Recibe la tarjeta, la encripta y hace POST al webhook
"""

import logging
import os
import httpx
import hmac
import secrets
import json
from datetime import datetime, timezone
from base64 import urlsafe_b64encode
from hashlib import sha256

from fastapi import APIRouter, Form, Request, Cookie, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from cryptography.fernet import Fernet  # type: ignore[import-untyped]

from app.infrastructure.redis_client import get_transaction_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tunnel", tags=["Tunnel"])

# ---------------------------------------------------------------------------
# Configuración del Túnel
# ---------------------------------------------------------------------------
_WEBHOOK_BASE_URL = os.getenv("ATLAS_TUNNEL_WEBHOOK_BASE_URL", "https://api.iamatlas.do")
_SECRET = os.getenv("ATLAS_TUNNEL_SECRET_KEY", "dummy-secret-key-for-tunnel-do-not-use-in-prod")

def _get_fernet() -> Fernet:
    """Deriva una llave compatible con Fernet a partir de un secreto."""
    key = urlsafe_b64encode(sha256(_SECRET.encode()).digest())
    return Fernet(key)

def _encrypt(value: str) -> str:
    """Encripta un string."""
    return _get_fernet().encrypt(value.encode()).decode()


# ---------------------------------------------------------------------------
# Validaciones
# ---------------------------------------------------------------------------
def _luhn_check(card_number: str) -> bool:
    digits = [int(d) for d in card_number]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# HTML Formulario (con resumen de cobro de Redis)
# ---------------------------------------------------------------------------
def _html_tunnel_form(
    theme: str = "light",
    error: str = "",
    csrf_token: str = "",
    ref: str = "",
    summary: dict | None = None,
) -> str:
    theme_class = "theme-dark" if theme == "dark" else "theme-light"
    error_block = f'<div class="error-msg" style="color:red; margin-bottom:15px;"> {error}</div>' if error else ""

    # Resumen del cobro (de Redis)
    summary_block = ""
    if summary:
        service_name = summary.get("service_name", "Servicio")
        date = summary.get("date", "")
        total = summary.get("total", "$0.00")

        # Construir filas del desglose
        detail_rows = ""
        if summary.get("taxes"):
            detail_rows += f"""
            <div class="summary-row">
              <span class="summary-label">Impuestos</span>
              <span class="summary-value">{summary['taxes']}</span>
            </div>"""
        if summary.get("technology_fee"):
            detail_rows += f"""
            <div class="summary-row">
              <span class="summary-label">Technology Fee</span>
              <span class="summary-value">{summary['technology_fee']}</span>
            </div>"""

        summary_block = f"""
    <div class="order-summary">
      <div class="summary-header">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#DA007C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14 2 14 8 20 8"></polyline>
          <line x1="16" y1="13" x2="8" y2="13"></line>
          <line x1="16" y1="17" x2="8" y2="17"></line>
        </svg>
        <span>Resumen del cobro</span>
      </div>

      <div class="summary-service">{service_name}</div>
      {"<div class='summary-date'>" + date + "</div>" if date else ""}

      {detail_rows}

      <div class="summary-divider"></div>
      <div class="summary-row summary-total">
        <span class="summary-label">Total</span>
        <span class="summary-value">{total}</span>
      </div>
    </div>
"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Túnel Seguro — Atlas</title>
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
  <link rel="stylesheet" href="/public/css/checkout.css?v=5">
  <style>
    /* ---- Resumen del cobro ---- */
    .order-summary {{
      background: linear-gradient(135deg, rgba(218,0,124,0.04), rgba(218,0,124,0.01));
      border: 1px solid rgba(218,0,124,0.12);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .summary-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.8rem;
      font-weight: 600;
      color: #DA007C;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
    }}
    .summary-service {{
      font-size: 1.05rem;
      font-weight: 700;
      color: var(--text-primary, #1a1a1a);
      margin-bottom: 4px;
    }}
    .summary-date {{
      font-size: 0.82rem;
      color: var(--text-secondary, #888);
      margin-bottom: 14px;
    }}
    .summary-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 4px 0;
    }}
    .summary-label {{
      font-size: 0.85rem;
      color: var(--text-secondary, #666);
    }}
    .summary-value {{
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text-primary, #1a1a1a);
    }}
    .summary-divider {{
      height: 1px;
      background: rgba(218,0,124,0.15);
      margin: 10px 0;
    }}
    .summary-total .summary-label {{
      font-weight: 700;
      font-size: 0.95rem;
      color: var(--text-primary, #1a1a1a);
    }}
    .summary-total .summary-value {{
      font-weight: 800;
      font-size: 1.1rem;
      color: #DA007C;
    }}

    /* Dark theme overrides */
    .theme-dark .summary-service {{ color: #f0f0f0; }}
    .theme-dark .summary-date {{ color: #999; }}
    .theme-dark .summary-label {{ color: #aaa; }}
    .theme-dark .summary-value {{ color: #f0f0f0; }}
    .theme-dark .summary-total .summary-label {{ color: #f0f0f0; }}
    .theme-dark .order-summary {{
      background: linear-gradient(135deg, rgba(218,0,124,0.08), rgba(218,0,124,0.02));
      border-color: rgba(218,0,124,0.2);
    }}
  </style>
</head>
<body class="{theme_class}">

<div class="checkout-layout" style="justify-content: center;">
  <div class="payment-container" style="max-width: 500px; margin: 40px auto; width: 100%;">
    <div class="header-section">
      <div class="icon-wrapper">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#DA007C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect>
          <line x1="1" y1="10" x2="23" y2="10"></line>
        </svg>
      </div>
      <div>
        <h2 class="title">Túnel de Pago Seguro</h2>
        <p class="subtitle">Completa tu pago de forma segura</p>
      </div>
    </div>

    {error_block}

    {summary_block}

    <!-- Visual Card Mockup -->
    <div class="visual-card">
      <svg class="card-icon" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect>
        <line x1="1" y1="10" x2="23" y2="10"></line>
      </svg>
      <div class="card-details">
        <div class="card-number" id="previewPan">•••• •••• •••• ••••</div>
        <div class="card-footer">
          <div class="card-holder" id="previewName">NOMBRE DEL TITULAR</div>
          <div class="card-expiry" id="previewExp">MM/AA</div>
        </div>
      </div>
      <div class="card-bg-decoration"></div>
      <div class="card-bottom-strip"></div>
    </div>

    <form id="tunnelForm" class="payment-form" method="POST" action="/tunnel/process" autocomplete="off">
      <input type="hidden" name="csrf_token" value="{csrf_token}"/>
      <input type="hidden" name="ref" value="{ref}"/>
      
      <div class="form-group">
        <label>Número de tarjeta</label>
        <div class="input-with-icon">
          <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line></svg>
          <input type="text" id="cardNumber" name="card_number"
                 class="signup-input with-icon"
                 placeholder="0000 0000 0000 0000"
                 maxlength="19" inputmode="numeric"
                 autocomplete="cc-number" required/>
        </div>
      </div>

      <div class="form-group">
        <label>Nombre del titular</label>
        <div class="input-with-icon">
          <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
          <input type="text" id="cardName" name="cardholder_name"
                 class="signup-input with-icon"
                 placeholder="Como aparece en la tarjeta"
                 maxlength="60" autocomplete="cc-name" required/>
        </div>
      </div>

      <div class="form-row">
        <div class="form-group half">
          <label>Vencimiento</label>
          <div class="input-with-icon">
            <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
            <input type="text" id="cardExp" name="expiration"
                   class="signup-input with-icon"
                   placeholder="MM/AA" maxlength="5"
                   inputmode="numeric" autocomplete="cc-exp" required/>
          </div>
        </div>
        <div class="form-group half">
          <label>CVV</label>
          <div class="input-with-icon">
            <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
            <input type="password" id="cardCvc" name="cvc"
                   class="signup-input with-icon"
                   placeholder="123" maxlength="4"
                   inputmode="numeric" autocomplete="cc-csc" required/>
          </div>
        </div>
      </div>

      <div class="form-group">
        <label>Código Postal</label>
        <div class="input-with-icon">
          <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg>
          <input type="text" id="postalCode" name="postal_code"
                 class="signup-input with-icon"
                 placeholder="10131" maxlength="10"
                 inputmode="numeric" autocomplete="postal-code" required/>
        </div>
      </div>

      <button type="submit" class="signup-btn-filled" id="submitBtn">
        <span id="btnText">Pagar de forma segura</span>
        <div class="spinner" id="spinner"></div>
      </button>
    </form>
  </div>
</div>

<script>
(function(){{
  'use strict';
  const cardInput = document.getElementById('cardNumber');
  const previewPan = document.getElementById('previewPan');
  const previewName = document.getElementById('previewName');
  const previewExp = document.getElementById('previewExp');

  cardInput.addEventListener('input', function() {{
    let v = this.value.replace(/\\D/g,'').slice(0,16);
    this.value = v.match(/.{{1,4}}/g)?.join(' ') || '';
    const masked = (v + '················').slice(0,16);
    previewPan.textContent = masked.match(/.{{1,4}}/g).join(' ');
  }});

  document.getElementById('cardName').addEventListener('input', function() {{
    previewName.textContent = this.value.toUpperCase() || 'NOMBRE DEL TITULAR';
  }});

  document.getElementById('cardExp').addEventListener('input', function() {{
    let v = this.value.replace(/\\D/g,'');
    if(v.length >= 2) v = v.slice(0,2) + '/' + v.slice(2,4);
    this.value = v;
    previewExp.textContent = v || 'MM/AA';
  }});

  function luhn(num) {{
    let sum=0, alt=false;
    for(let i=num.length-1;i>=0;i--) {{
      let n=parseInt(num[i],10);
      if(alt){{ n*=2; if(n>9) n-=9; }}
      sum+=n; alt=!alt;
    }}
    return sum%10===0;
  }}

  document.getElementById('tunnelForm').addEventListener('submit', function(e) {{
    e.preventDefault();
    const raw = cardInput.value.replace(/\\s/g,'');
    if(raw.length < 15) {{ alert('Número de tarjeta inválido.'); return; }}
    if(!luhn(raw)) {{ alert('Número de tarjeta inválido (verificación Luhn fallida).'); return; }}

    const exp = document.getElementById('cardExp').value;
    const [mm,yy] = exp.split('/');
    if(!mm||!yy||mm<1||mm>12) {{ alert('Fecha de vencimiento inválida.'); return; }}
    const now = new Date();
    const expFull = new Date(2000+parseInt(yy), parseInt(mm)-1, 1);
    if(expFull < new Date(now.getFullYear(), now.getMonth(), 1)) {{
      alert('Tu tarjeta está vencida.'); return;
    }}

    const cvc = document.getElementById('cardCvc').value;
    if(cvc.length < 3) {{ alert('CVC inválido.'); return; }}

    const postal = document.getElementById('postalCode').value.trim();
    if(!postal) {{ alert('Código postal es requerido.'); return; }}

    document.getElementById('btnText').style.display = 'none';
    document.getElementById('spinner').style.display = 'block';
    document.getElementById('submitBtn').disabled = true;

    this.submit();
  }});
}})();
</script>
</body>
</html>
"""

def _html_tunnel_success(theme: str = "light") -> str:
    theme_class = "theme-dark" if theme == "dark" else "theme-light"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Proceso Exitoso — Atlas</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/public/css/checkout.css">
<style>
  body {{ justify-content: center; align-items: center; min-height: 100vh; }}
  .result-card {{ background: white; border-radius: 16px; padding: 40px 32px 32px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.05); color: #1a1a1a; max-width: 400px; margin: 0 auto; }}
  .result-icon-wrap {{ width: 80px; height: 80px; border-radius: 50%; background: rgba(16, 185, 129, 0.06); border: 1.5px solid rgba(16, 185, 129, 0.2); display: flex; align-items: center; justify-content: center; margin: 0 auto 24px; }}
  .check-anim {{ stroke-dasharray: 32; stroke-dashoffset: 32; animation: draw 0.5s 0.3s ease-out forwards; }}
  @keyframes draw {{ to {{ stroke-dashoffset: 0; }} }}
  .result-status-title {{ font-size: 1.4rem; font-weight: 700; color: #10b981; margin-bottom: 8px; }}
  .result-status-subtitle {{ font-size: 0.85rem; color: #666; line-height: 1.6; margin-bottom: 28px; }}
</style>
</head>
<body class="{theme_class}">
  <div class="result-card">
    <div class="result-icon-wrap">
        <svg width="56" height="56" viewBox="0 0 56 56" fill="none">
            <circle cx="28" cy="28" r="26" stroke="#10b981" stroke-width="2.5" fill="rgba(16,185,129,0.06)"/>
            <path d="M18 29 L24 35 L38 21" stroke="#10b981" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="check-anim"/>
        </svg>
    </div>
    <h1 class="result-status-title">Pago en proceso</h1>
    <p class="result-status-subtitle">Estamos confirmando tu reserva. Recibirás una notificación cuando esté lista.</p>
  </div>
</body>
</html>
"""


def _html_tunnel_error(theme: str = "light", message: str = "") -> str:
    """Página de error cuando el ref no existe o es inválido."""
    theme_class = "theme-dark" if theme == "dark" else "theme-light"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Enlace inválido — Atlas</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/public/css/checkout.css">
<style>
  body {{ justify-content: center; align-items: center; min-height: 100vh; }}
  .result-card {{ background: white; border-radius: 16px; padding: 40px 32px 32px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.05); color: #1a1a1a; max-width: 400px; margin: 0 auto; }}
  .result-icon-wrap {{ width: 80px; height: 80px; border-radius: 50%; background: rgba(239, 68, 68, 0.06); border: 1.5px solid rgba(239, 68, 68, 0.2); display: flex; align-items: center; justify-content: center; margin: 0 auto 24px; }}
  .result-status-title {{ font-size: 1.4rem; font-weight: 700; color: #ef4444; margin-bottom: 8px; }}
  .result-status-subtitle {{ font-size: 0.85rem; color: #666; line-height: 1.6; margin-bottom: 28px; }}
</style>
</head>
<body class="{theme_class}">
  <div class="result-card">
    <div class="result-icon-wrap">
        <svg width="56" height="56" viewBox="0 0 56 56" fill="none">
            <circle cx="28" cy="28" r="26" stroke="#ef4444" stroke-width="2.5" fill="rgba(239,68,68,0.06)"/>
            <line x1="20" y1="20" x2="36" y2="36" stroke="#ef4444" stroke-width="3" stroke-linecap="round"/>
            <line x1="36" y1="20" x2="20" y2="36" stroke="#ef4444" stroke-width="3" stroke-linecap="round"/>
        </svg>
    </div>
    <h1 class="result-status-title">Enlace no válido</h1>
    <p class="result-status-subtitle">{message or "Este enlace de pago es inválido o ha expirado. Solicita un nuevo enlace."}</p>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
async def tunnel_get(request: Request, ref: str = "", error: str = ""):
    """Renderiza el formulario visual del túnel con resumen de cobro."""
    theme = request.cookies.get("theme", "light").lower().strip().strip('"').strip("'")
    if theme not in ("dark", "light"):
        theme = "light"

    # El parámetro ref es obligatorio
    if not ref:
        logger.warning("[tunnel] Acceso sin parámetro ref.")
        return HTMLResponse(_html_tunnel_error(theme=theme, message="Enlace de pago incompleto. Falta la referencia de la transacción."), status_code=400)

    # Leer resumen del cobro desde Redis
    summary = await get_transaction_summary(ref)
    if summary is None:
        logger.warning("[tunnel] No se encontró resumen en Redis para ref=%s", ref)
        return HTMLResponse(_html_tunnel_error(theme=theme, message="Este enlace de pago es inválido o ha expirado. Solicita un nuevo enlace de pago."), status_code=404)

    # Generar Token CSRF
    csrf_token = secrets.token_hex(32)
    response = HTMLResponse(_html_tunnel_form(
        theme=theme,
        error=error,
        csrf_token=csrf_token,
        ref=ref,
        summary=summary,
    ))
    
    # Setear cookie segura (HttpOnly impide acceso vía JavaScript cruzado)
    response.set_cookie(
        "tunnel_csrf", 
        csrf_token, 
        httponly=True, 
        secure=request.url.scheme == "https", 
        samesite="lax", 
        max_age=3600
    )
    return response


@router.post("/process", response_class=HTMLResponse)
async def tunnel_process(
    request: Request,
    card_number: str = Form(...),
    cardholder_name: str = Form(...),
    expiration: str = Form(...),
    cvc: str = Form(...),
    postal_code: str = Form(...),
    ref: str = Form(""),
    csrf_token: str = Form(""),
    tunnel_csrf: str = Cookie(None)
):
    """Recibe datos del formulario, encripta y envía al webhook (SIN guardar en DB)."""
    theme = request.cookies.get("theme", "light").lower().strip().strip('"').strip("'")
    if theme not in ("dark", "light"):
        theme = "light"

    def _render_error(msg: str):
        new_csrf = secrets.token_hex(32)
        resp = HTMLResponse(_html_tunnel_form(theme=theme, error=msg, csrf_token=new_csrf, ref=ref), status_code=400)
        resp.set_cookie("tunnel_csrf", new_csrf, httponly=True, secure=request.url.scheme == "https", samesite="lax", max_age=3600)
        return resp

    # Validación del ref
    if not ref:
        logger.warning("[tunnel] POST sin parámetro ref.")
        return HTMLResponse(_html_tunnel_error(theme=theme, message="Referencia de transacción faltante."), status_code=400)

    # Validación CSRF (Mitigación Cross-Site Request Forgery)
    if not tunnel_csrf or not csrf_token or not secrets.compare_digest(csrf_token, tunnel_csrf):
        logger.warning("[tunnel] Intento bloqueado por fallo en validación CSRF.")
        return _render_error("Sesión de seguridad expirada. Por favor, intenta de nuevo.")

    card_number_clean = card_number.replace(" ", "")
    
    # Validaciones Backend
    if not card_number_clean.isdigit() or not _luhn_check(card_number_clean):
        return _render_error("Número de tarjeta inválido")
    
    if "/" in expiration:
        mm, yy = expiration.split("/")
        expiration_clean = f"20{yy}{mm}"
    else:
        expiration_clean = expiration
    
    if not cvc.isdigit():
        return _render_error("CVC inválido")

    postal_code_clean = postal_code.strip()
    if not postal_code_clean:
        return _render_error("Código postal es requerido")

    # Encriptación del Payload
    payload = {
        "card_number_enc": _encrypt(card_number_clean),
        "cvc_enc": _encrypt(cvc),
        "expiration": expiration_clean,
        "cardholder_name": cardholder_name,
        "postal_code": postal_code_clean,
    }

    # Firma HMAC (Autenticidad para el Webhook)
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode()
    signature = hmac.new(_SECRET.encode(), payload_bytes, sha256).hexdigest()
    
    headers = {
        "X-Atlas-Signature": signature,
        "Content-Type": "application/json"
    }

    # Construir URL del Webhook con el ref
    webhook_url = f"{_WEBHOOK_BASE_URL.rstrip('/')}/payments/tunnel-webhook/{ref}"

    # Llamada al Webhook
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, content=payload_bytes, headers=headers, timeout=10.0)
            if resp.status_code >= 400:
                logger.error(f"[tunnel] Webhook respondió con error: {resp.status_code} - {resp.text}")
                return _render_error("El sistema rechazó la petición. Intente más tarde.")
    except Exception as e:
        logger.error(f"[tunnel] Error contactando al webhook: {str(e)}")
        return _render_error("Ocurrió un error de comunicación temporal. Intente más tarde.")

    logger.info(f"[tunnel] Tarjeta (last4={card_number_clean[-4:]}) procesada y enviada a {webhook_url}.")
    return HTMLResponse(_html_tunnel_success(theme=theme))
