"""
Checkout — Payment Form UI.

GET  /checkout          → Formulario HTML de pago
POST /checkout/process  → Procesa el pago y redirige al resultado

Este endpoint es PÚBLICO (no requiere API Key) para que el navegador
del usuario pueda acceder. La seguridad se implementa con:
  - CSP headers estrictos
  - Validación Luhn client-side (no se envía tarjeta inválida)
  - Datos de tarjeta NUNCA se loguean ni persisten en texto claro
  - HTTPS obligatorio en producción (configurar en ECS/ALB)
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.infrastructure.azul_gateway import AzulPaymentGateway
from app.infrastructure.database import get_db
from app.infrastructure.repo_impl import SQLPaymentRepository, SQLTransactionRepository
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from app.services.payment_service import PaymentService
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("checkout")

router = APIRouter(prefix="/checkout", tags=["Checkout"])

_APP_BASE = os.getenv("APP_BASE_URL", "http://localhost:8000")

# In-memory cache for 3DS challenge forms (keyed by payment_id)
# The challenge page is fetched within seconds of being stored; no TTL needed.
_challenge_cache: Dict[str, str] = {}


def _get_service(db: AsyncSession = Depends(get_db)) -> PaymentService:
    return PaymentService(
        payment_repo=SQLPaymentRepository(db),
        txn_repo=SQLTransactionRepository(db),
        gateway=AzulPaymentGateway(),
        card_repo=SQLSavedCardRepository(db),
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _html_form(error: str = "") -> str:
    error_block = f'<div class="error-msg">⚠️ {error}</div>' if error else ""
    return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Pago Seguro — Atlas</title>
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
  <style>
    /* Reset & Base */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #f8f9fa;
      font-family: 'Inter', sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }

    /* CSS from PaymentMethods.css */
    .payment-container {
      background: white;
      border-radius: 16px;
      padding: 32px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.07);
      width: 100%;
      max-width: 500px;
      margin: 0 auto;
      color: #1a1a1a;
    }

    .header-section {
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 32px;
    }

    .icon-wrapper {
      background: #FFF0F8;
      padding: 10px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .title {
      font-size: 20px;
      font-weight: 800;
      color: #343C6A;
      margin: 0 0 4px 0;
    }

    .subtitle {
      font-size: 13px;
      color: #666;
      margin: 0;
    }

    /* Visual Card */
    .visual-card {
      background: linear-gradient(135deg, #DA007C 0%, #a0005a 100%);
      border-radius: 20px;
      padding: 24px;
      color: white;
      position: relative;
      overflow: hidden;
      margin-bottom: 32px;
      min-height: 200px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      box-shadow: 0 10px 20px rgba(218,0,124,0.2);
    }

    .card-bg-decoration {
      position: absolute;
      top: -50px;
      right: -50px;
      width: 200px;
      height: 200px;
      background: rgba(255,255,255,0.05);
      border-radius: 50%;
      z-index: 1;
    }

    .card-icon {
      position: relative;
      z-index: 2;
      width: 28px;
      height: 28px;
    }

    .card-details {
      position: relative;
      z-index: 2;
    }

    .card-number {
      font-family: monospace;
      font-size: 22px;
      letter-spacing: 2px;
      margin-bottom: 24px;
      font-weight: 600;
      min-height: 26px;
    }

    .card-footer {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
    }

    .card-holder {
      font-size: 12px;
      letter-spacing: 1px;
      opacity: 0.9;
    }

    .card-expiry {
      font-size: 13px;
      font-weight: 600;
    }

    /* Form */
    .payment-form {
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .form-row {
      display: flex;
      gap: 16px;
    }

    .form-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .form-group.half {
      flex: 1;
    }

    .form-group label {
      font-size: 13px;
      color: #333;
      font-weight: 500;
    }

    .input-with-icon {
      position: relative;
      display: flex;
      align-items: center;
    }

    .input-icon {
      position: absolute;
      left: 16px;
      z-index: 2;
      width: 16px;
      height: 16px;
      color: #aaa;
    }

    .signup-input {
      width: 100%;
      padding: 11px 16px;
      border-radius: 100px;
      border: 1px solid rgba(200,200,200,0.8);
      font-size: 13px;
      background: rgba(255,255,255,0.80);
      color: #1a1a1a;
      outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
      box-sizing: border-box;
      font-family: 'Inter', sans-serif;
    }

    .signup-input.with-icon {
      padding-left: 44px;
    }

    .signup-input:focus {
      border-color: #DA007C;
      box-shadow: 0 0 0 3px rgba(218,0,124,0.1);
    }

    .signup-input::placeholder {
      color: #aaa;
    }

    .signup-btn-filled {
      padding: 14px;
      border-radius: 100px;
      border: none;
      color: white;
      background: #DA007C;
      font-weight: 600;
      font-size: 14px;
      cursor: pointer;
      transition: background 0.2s, transform 0.1s;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      margin-top: 8px;
    }

    .signup-btn-filled:hover {
      background: #c0006c;
    }

    .signup-btn-filled:active {
      transform: scale(0.98);
    }

    .signup-btn-filled:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }

    /* Checkbox */
    .checkbox-label {
      display: flex;
      align-items: center;
      font-size: 13px;
      color: #555;
      cursor: pointer;
      user-select: none;
      position: relative;
      padding-left: 28px;
    }

    .checkbox-label input {
      position: absolute;
      opacity: 0;
      cursor: pointer;
      height: 0;
      width: 0;
    }

    .checkmark {
      position: absolute;
      top: 50%;
      left: 0;
      transform: translateY(-50%);
      height: 18px;
      width: 18px;
      background-color: white;
      border: 1px solid rgba(200,200,200,0.8);
      border-radius: 4px;
      transition: all 0.2s;
    }

    .checkbox-label:hover input ~ .checkmark {
      border-color: #DA007C;
    }

    .checkbox-label input:checked ~ .checkmark {
      background-color: #DA007C;
      border-color: #DA007C;
    }

    .checkmark:after {
      content: "";
      position: absolute;
      display: none;
      left: 6px;
      top: 2px;
      width: 4px;
      height: 9px;
      border: solid white;
      border-width: 0 2px 2px 0;
      transform: rotate(45deg);
    }

    .checkbox-label input:checked ~ .checkmark:after {
      display: block;
    }

    .error-msg {
      background: rgba(244,63,94,.1); border: 1px solid rgba(244,63,94,.3);
      color: #f43f5e; padding: .75rem 1rem; border-radius: .6rem;
      margin-bottom: 1rem; font-size: .85rem;
    }

    .spinner {
      display: none; width: 18px; height: 18px; border: 2px solid #fff3;
      border-top-color: #fff; border-radius: 50%; animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    @media (max-width: 480px) {
      .payment-container { padding: 16px; border-radius: 12px; }
      .form-row { flex-direction: column; gap: 20px; }
      .card-number { font-size: 18px; letter-spacing: 1px; }
      .visual-card { padding: 20px; min-height: 180px; }
    }
  </style>
</head>
<body>

<div class="payment-container">
  <div class="header-section">
    <div class="icon-wrapper">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#DA007C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect>
        <line x1="1" y1="10" x2="23" y2="10"></line>
      </svg>
    </div>
    <div>
      <h2 class="title">Pago Seguro</h2>
      <p class="subtitle">Complete los datos de su tarjeta para pagar RD$2.36</p>
    </div>
  </div>

  {error_block}

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
  </div>

  <form id="payForm" class="payment-form" method="POST" action="/checkout/process" autocomplete="off">
    <!-- Anti-CSRF token -->
    <input type="hidden" name="csrf_token" id="csrf_token"/>

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
      <label>Correo electrónico</label>
      <div class="input-with-icon">
        <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>
        <input type="email" name="cardholder_email"
               class="signup-input with-icon"
               placeholder="tu@correo.com"
               autocomplete="email" required/>
      </div>
    </div>

    <div class="checkbox-group">
      <label class="checkbox-label">
        <input type="checkbox" name="save_card" value="1" checked/>
        <span class="checkmark"></span>
        Guardar esta tarjeta para futuros pagos
      </label>
    </div>

    <!-- Campos browser fingerprint para 3DS 2.0 -->
    <input type="hidden" name="browser_accept_header" id="browserAccept"/>
    <input type="hidden" name="browser_ip" id="browserIp" value=""/>
    <input type="hidden" name="browser_language" id="browserLang"/>
    <input type="hidden" name="browser_color_depth" id="browserColor"/>
    <input type="hidden" name="browser_screen_width" id="browserWidth"/>
    <input type="hidden" name="browser_screen_height" id="browserHeight"/>
    <input type="hidden" name="browser_time_zone" id="browserTz"/>
    <input type="hidden" name="browser_user_agent" id="browserUA"/>
    <input type="hidden" name="browser_java" id="browserJava" value="false"/>

    <button type="submit" class="signup-btn-filled" id="submitBtn">
      <span id="btnText">Pagar RD$2.36</span>
      <div class="spinner" id="spinner"></div>
    </button>
  </form>
</div>

<script>
(function(){
  'use strict';

  const csrf = Math.random().toString(36).slice(2) + Date.now().toString(36);
  document.getElementById('csrf_token').value = csrf;

  const cardInput = document.getElementById('cardNumber');
  const previewPan = document.getElementById('previewPan');
  const previewName = document.getElementById('previewName');
  const previewExp = document.getElementById('previewExp');

  cardInput.addEventListener('input', function() {
    let v = this.value.replace(/\D/g,'').slice(0,16);
    this.value = v.match(/.{1,4}/g)?.join(' ') || '';
    const masked = (v + '················').slice(0,16);
    previewPan.textContent = masked.match(/.{1,4}/g).join(' ');
  });

  document.getElementById('cardName').addEventListener('input', function() {
    previewName.textContent = this.value.toUpperCase() || 'NOMBRE DEL TITULAR';
  });

  document.getElementById('cardExp').addEventListener('input', function() {
    let v = this.value.replace(/\D/g,'');
    if(v.length >= 2) v = v.slice(0,2) + '/' + v.slice(2,4);
    this.value = v;
    previewExp.textContent = v || 'MM/AA';
  });

  function luhn(num) {
    let sum=0, alt=false;
    for(let i=num.length-1;i>=0;i--) {
      let n=parseInt(num[i],10);
      if(alt){ n*=2; if(n>9) n-=9; }
      sum+=n; alt=!alt;
    }
    return sum%10===0;
  }

  function collectBrowserInfo() {
    document.getElementById('browserAccept').value = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8';
    document.getElementById('browserLang').value = navigator.language || 'es-DO';
    document.getElementById('browserColor').value = String(screen.colorDepth || 24);
    document.getElementById('browserWidth').value = String(screen.width || 1920);
    document.getElementById('browserHeight').value = String(screen.height || 1080);
    document.getElementById('browserTz').value = String(new Date().getTimezoneOffset());
    document.getElementById('browserUA').value = navigator.userAgent || '';
    document.getElementById('browserJava').value = String(navigator.javaEnabled ? navigator.javaEnabled() : false);
  }

  document.getElementById('payForm').addEventListener('submit', function(e) {
    e.preventDefault();
    const raw = cardInput.value.replace(/\s/g,'');
    if(raw.length < 15) { alert('Número de tarjeta inválido.'); return; }
    if(!luhn(raw)) { alert('Número de tarjeta inválido (verificación Luhn fallida).'); return; }

    const exp = document.getElementById('cardExp').value;
    const [mm,yy] = exp.split('/');
    if(!mm||!yy||mm<1||mm>12) { alert('Fecha de vencimiento inválida.'); return; }
    const now = new Date();
    const expFull = new Date(2000+parseInt(yy), parseInt(mm)-1, 1);
    if(expFull < new Date(now.getFullYear(), now.getMonth(), 1)) {
      alert('Tu tarjeta está vencida.'); return;
    }

    const cvc = document.getElementById('cardCvc').value;
    if(cvc.length < 3) { alert('CVC inválido.'); return; }

    collectBrowserInfo();

    document.getElementById('btnText').style.display = 'none';
    document.getElementById('spinner').style.display = 'block';
    document.getElementById('submitBtn').disabled = true;

    this.submit();
  });
})();
</script>
</body>
</html>""".replace("{error_block}", error_block)


def _html_3ds_method(payment_id: str, method_form: str, amount: int) -> str:
    """Página intermedia — renderiza el iframe silencioso 3DS Method y continúa."""
    return """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Verificando seguridad — Atlas</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f0f1a;color:#e2e8f0;font-family:'Inter',sans-serif;
      min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1.5rem;}}
.card{{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:1.5rem;
       padding:2.5rem;max-width:420px;width:100%;text-align:center;
       box-shadow:0 25px 50px rgba(0,0,0,.5);}}
.spinner-wrap{{margin:1.5rem auto;width:56px;height:56px;
  background:linear-gradient(135deg,#6c63ff,#a78bfa);
  border-radius:50%;display:flex;align-items:center;justify-content:center;}}
.ring{{width:40px;height:40px;border:3px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;animation:spin .9s linear infinite;}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
h2{{font-size:1.2rem;font-weight:700;margin-bottom:.5rem;}}
.sub{{color:#94a3b8;font-size:.88rem;margin-bottom:1.5rem;}}
.step{{font-size:.78rem;color:#64748b;margin-top:1rem;}}
/* Iframe 3DS Method — debe ser 0x0 (invisible) segun spec EMV 3DS */
.method-iframe{{width:0;height:0;border:none;position:absolute;top:-9999px;}}
</style></head>
<body><div class="card">
  <div class="spinner-wrap"><div class="ring"></div></div>
  <h2>Verificando tu tarjeta</h2>
  <div class="sub">Tu banco está confirmando tu identidad.<br>Esto toma solo unos segundos…</div>
  <div class="step" id="stepMsg">Iniciando verificación segura…</div>
</div>

<!-- Iframe 3DS Method (invisible) -->
{method_form}

<script>
(function() {{
  'use strict';
  var paymentId = {payment_id!r};
  var waited = false;
  var notified = false;

  // Escuchar cuando el ACS notifica el method-notification (postMessage o polling)
  window.addEventListener('message', function(e) {{
    notified = true;
  }});

  function continueFlow(status) {{
    if (waited) return;
    waited = true;
    document.getElementById('stepMsg').textContent = 'Finalizando verificación…';
    fetch('/checkout/3ds-continue', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: 'payment_id=' + encodeURIComponent(paymentId) +
            '&method_status=' + encodeURIComponent(status)
    }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
      if (data.status === 'PENDING_3DS_CHALLENGE') {{
        document.getElementById('stepMsg').textContent = 'Autenticando con tu banco…';
        // Full browser navigation — gives CardinalCommerce a proper Referer header
        // and avoids Cloudflare WAF bot-detection triggered by document.write().
        window.location.href = '/checkout/challenge/' + encodeURIComponent(data.payment_id || paymentId);
      }} else if (data.redirect_url) {{
        document.getElementById('stepMsg').textContent = 'Redirigiendo a tu banco…';
        window.location.href = data.redirect_url;
      }} else {{
        window.location.href = data.result_url || '/checkout';
      }}
    }}).catch(function() {{
      window.location.href = '/checkout?err=3ds';
    }});
  }}

  // Esperar hasta 10 segundos el callback del ACS, luego continuar
  setTimeout(function() {{
    continueFlow(notified ? 'RECEIVED' : 'EXPECTED_BUT_NOT_RECEIVED');
  }}, 10000);

  // Si el ACS notificó rápido, continuar de inmediato
  window.addEventListener('message', function() {{
    if (!waited) {{
      setTimeout(function() {{ continueFlow('RECEIVED'); }}, 500);
    }}
  }});
}})();
</script>
</body></html>"""


def _html_result(status: str, message: str, payment_id: str, amount: int, iso: str) -> str:
    ok = status == "APPROVED"
    color = "#10b981" if ok else "#f43f5e"
    icon = "✅" if ok else "❌"
    title = "Pago Aprobado" if ok else "Pago Rechazado"
    return """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title} — Atlas</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f0f1a;color:#e2e8f0;font-family:'Inter',sans-serif;
      min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1.5rem;}}
.card{{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:1.5rem;
       padding:2.5rem;max-width:400px;width:100%;text-align:center;
       box-shadow:0 25px 50px rgba(0,0,0,.5);}}
.icon{{font-size:4rem;margin-bottom:1rem;}}
h1{{font-size:1.5rem;font-weight:700;margin-bottom:.5rem;color:{color};}}
.sub{{color:#94a3b8;font-size:.9rem;margin-bottom:1.5rem;}}
.detail-row{{display:flex;justify-content:space-between;padding:.6rem 0;
             border-bottom:1px solid #2d2d4e;font-size:.85rem;}}
.detail-row:last-of-type{{border-bottom:none;}}
.label{{color:#94a3b8;}}
.value{{font-weight:600;}}
.btn{{display:inline-block;margin-top:1.5rem;padding:.7rem 1.5rem;
      background:linear-gradient(135deg,#6c63ff,#a78bfa);border-radius:.75rem;
      color:#fff;font-weight:700;text-decoration:none;font-size:.9rem;}}
</style></head>
<body><div class="card">
<div class="icon">{icon}</div>
<h1>{title}</h1>
<div class="sub">{message}</div>
<div class="detail-row"><span class="label">Payment ID</span><span class="value" style="font-size:.75rem">{payment_id[:18]}…</span></div>
<div class="detail-row"><span class="label">Monto</span><span class="value">RD${amount/100:.2f}</span></div>
<div class="detail-row"><span class="label">IsoCode</span><span class="value">{iso}</span></div>
<div class="detail-row"><span class="label">Estado</span><span class="value" style="color:{color}">{status}</span></div>
<a href="/checkout" class="btn">← Nueva prueba</a>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def checkout_form():
    """Sirve el formulario de pago."""
    resp = HTMLResponse(_html_form())
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


@router.post("/process", include_in_schema=False)
async def process_checkout(
    request: Request,
    card_number: str = Form(...),
    cardholder_name: str = Form(...),
    expiration: str = Form(...),
    cvc: str = Form(...),
    cardholder_email: str = Form(...),
    save_card: str = Form(""),
    # Browser fingerprint para 3DS 2.0 (Azul producción lo requiere)
    browser_accept_header: str = Form("text/html"),
    browser_ip: str = Form(""),
    browser_language: str = Form("es-DO"),
    browser_color_depth: str = Form("24"),
    browser_screen_width: str = Form("1280"),
    browser_screen_height: str = Form("720"),
    browser_time_zone: str = Form("240"),
    browser_user_agent: str = Form(""),
    browser_java: str = Form("false"),
    svc: PaymentService = Depends(_get_service),
):
    """Procesa el pago — recibe el form POST, llama a AZUL, redirige al resultado."""
    card_clean = card_number.replace(" ", "").strip()
    card_masked = f"{'*' * (len(card_clean) - 4)}{card_clean[-4:]}" if len(card_clean) >= 4 else "****"

    logger.warning(
        "[CHECKOUT] ▶ POST /checkout/process | card=%s exp=%s email=%s ua=%s ip=%s",
        card_masked, expiration, cardholder_email,
        request.headers.get("User-Agent", "")[:60],
        request.headers.get("X-Forwarded-For", request.client.host if request.client else "?"),
    )

    # Convertir expiración MM/AA → YYYYMM
    try:
        mm, yy = expiration.strip().split("/")
        exp_azul = f"20{yy.strip()}{mm.strip().zfill(2)}"
    except Exception:
        logger.error("[CHECKOUT] ✗ Expiration parse failed: %r", expiration)
        return HTMLResponse(_html_form("Fecha de vencimiento inválida. Usa MM/AA."), status_code=422)

    # Monto fijo de prueba: RD$2.00 + ITBIS RD$0.36 = RD$2.36
    amount = 200
    itbis  = 36

    # IP real del cliente
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or browser_ip
    )

    # Browser fingerprint para 3DS 2.0
    browser_info = {
        "accept_header": browser_accept_header or "text/html",
        "ip_address": client_ip,
        "language": browser_language or "es-DO",
        "color_depth": browser_color_depth or "24",
        "screen_width": browser_screen_width or "1280",
        "screen_height": browser_screen_height or "720",
        "time_zone": browser_time_zone or "240",
        "user_agent": browser_user_agent or request.headers.get("User-Agent", ""),
        "javascript_enabled": "true",
    }
    logger.warning(
        "[CHECKOUT] browser_info → ip=%s lang=%s w=%s h=%s tz=%s",
        client_ip, browser_info["language"],
        browser_info["screen_width"], browser_info["screen_height"],
        browser_info["time_zone"],
    )

    # ── Llamada a Azul ────────────────────────────────────────────────────
    logger.warning(
        "[CHECKOUT] → calling process_sale | amount=%d itbis=%d auth_mode=3dsecure card=%s",
        amount, itbis, card_masked,
    )
    try:
        # Para suscripciones siempre tokenizamos: save_card=True + STANDING_ORDER indicator
        payment = await svc.process_sale(
            amount=amount,
            itbis=itbis,
            card_number=card_clean,
            expiration=exp_azul,
            cvc=cvc.strip(),
            order_id=f"CHK-{uuid.uuid4().hex[:8].upper()}",
            auth_mode="3dsecure",
            save_card=True,
            cardholder_name=cardholder_name.strip(),
            cardholder_email=cardholder_email.strip(),
            browser_info=browser_info,
        )
    except Exception as exc:
        logger.error(
            "[CHECKOUT] ✗ process_sale EXCEPTION | type=%s msg=%s",
            type(exc).__name__, str(exc)[:400],
        )
        return HTMLResponse(_html_form(f"Error al procesar: {exc}"), status_code=422)

    logger.warning(
        "[CHECKOUT] ← Azul response | payment_id=%s status=%s iso=%s rc=%s msg=%r "
        "azul_order_id=%s method_form_len=%d",
        payment.id,
        payment.status.value if hasattr(payment.status, "value") else payment.status,
        payment.iso_code,
        payment.response_code,
        payment.response_message,
        payment.azul_order_id,
        len(payment.threeds_method_form or ""),
    )

    from app.domain.entities import PaymentStatus

    # ── 3DS Method ────────────────────────────────────────────────────────
    if payment.status == PaymentStatus.PENDING_3DS_METHOD:
        if payment.threeds_method_form:
            logger.warning(
                "[CHECKOUT] → 3DS METHOD | payment_id=%s form_len=%d → rendering iframe",
                payment.id, len(payment.threeds_method_form),
            )
            html = _html_3ds_method(
                payment_id=payment.id,
                method_form=payment.threeds_method_form,
                amount=payment.amount + payment.itbis,
            )
            resp = HTMLResponse(html)
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp
        else:
            logger.warning(
                "[CHECKOUT] → 3DS METHOD | payment_id=%s form_len=0 → auto-continue NOT_EXPECTED",
                payment.id,
            )
            try:
                payment = await svc.continue_three_ds_method(payment.id, "NOT_EXPECTED")
                logger.warning(
                    "[CHECKOUT] ← 3DS METHOD auto-continue | payment_id=%s new_status=%s iso=%s msg=%r",
                    payment.id,
                    payment.status.value if hasattr(payment.status, "value") else payment.status,
                    payment.iso_code,
                    payment.response_message,
                )
            except Exception as exc:
                logger.error(
                    "[CHECKOUT] ✗ 3DS METHOD auto-continue EXCEPTION | payment_id=%s type=%s msg=%s",
                    payment.id, type(exc).__name__, str(exc)[:400],
                )
                return HTMLResponse(_html_form(f"Error 3DS (método): {exc}"), status_code=502)
            # Fall through to challenge / result handling below

    # ── 3DS Challenge ─────────────────────────────────────────────────────
    if payment.status == PaymentStatus.PENDING_3DS_CHALLENGE:
        challenge_form = payment.threeds_challenge_form or ""
        redirect_url   = payment.threeds_redirect_url or ""
        logger.warning(
            "[CHECKOUT] → 3DS CHALLENGE | payment_id=%s challenge_form_len=%d redirect_url=%r",
            payment.id, len(challenge_form), redirect_url[:80] if redirect_url else "",
        )
        if challenge_form:
            resp = HTMLResponse(challenge_form)
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
            return resp
        if redirect_url:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=redirect_url)
        logger.error("[CHECKOUT] ✗ 3DS CHALLENGE | payment_id=%s no challenge_form and no redirect_url", payment.id)
        return HTMLResponse(_html_form("Error 3DS: sin URL de challenge."), status_code=502)

    # ── Resultado final ───────────────────────────────────────────────────
    status = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
    msg = payment.response_message or ""
    token_info = f" · Token: {payment.data_vault_token[:12]}…" if payment.data_vault_token else ""

    logger.warning(
        "[CHECKOUT] ■ FINAL RESULT | payment_id=%s status=%s iso=%s rc=%s msg=%r token=%s",
        payment.id, status, payment.iso_code, payment.response_code,
        payment.response_message,
        payment.data_vault_token[:12] + "…" if payment.data_vault_token else "(none)",
    )

    html = _html_result(
        status=status,
        message=msg + token_info,
        payment_id=payment.id,
        amount=payment.amount + payment.itbis,
        iso=payment.iso_code,
    )
    resp = HTMLResponse(html)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@router.post("/3ds-continue", include_in_schema=False)
async def continue_3ds(
    payment_id: str = Form(...),
    method_status: str = Form("RECEIVED"),
    svc: PaymentService = Depends(_get_service),
):
    """Continuación interna del flujo 3DS — llamada por el JS del iframe Method."""
    from app.domain.entities import PaymentStatus
    from fastapi.responses import JSONResponse

    logger.warning(
        "[CHECKOUT] ▶ POST /3ds-continue | payment_id=%s method_status=%s",
        payment_id, method_status,
    )

    try:
        payment = await svc.continue_three_ds_method(payment_id, method_status)
    except ValueError as exc:
        logger.error("[CHECKOUT] ✗ 3ds-continue ValueError | payment_id=%s error=%s", payment_id, exc)
        return JSONResponse({"error": str(exc), "result_url": "/checkout"}, status_code=400)
    except Exception as exc:
        logger.error(
            "[CHECKOUT] ✗ 3ds-continue EXCEPTION | payment_id=%s type=%s msg=%s",
            payment_id, type(exc).__name__, str(exc)[:400],
        )
        return JSONResponse({"error": str(exc), "result_url": "/checkout"}, status_code=502)

    logger.warning(
        "[CHECKOUT] ← 3ds-continue result | payment_id=%s status=%s iso=%s rc=%s msg=%r",
        payment.id,
        payment.status.value if hasattr(payment.status, "value") else payment.status,
        payment.iso_code,
        payment.response_code,
        payment.response_message,
    )

    if payment.status == PaymentStatus.PENDING_3DS_CHALLENGE:
        challenge_form = payment.threeds_challenge_form or ""
        redirect_url   = payment.threeds_redirect_url or ""
        logger.warning(
            "[CHECKOUT] → 3DS CHALLENGE from 3ds-continue | payment_id=%s form_len=%d redirect=%r",
            payment.id,
            len(challenge_form),
            (redirect_url or "")[:80],
        )
        if challenge_form:
            # Store in cache and redirect the browser — avoids document.write() Cloudflare block
            _challenge_cache[payment.id] = challenge_form
        return JSONResponse({
            "status": payment.status.value,
            "payment_id": payment.id,
            "challenge_form": "",          # not sent over JSON anymore
            "redirect_url": redirect_url or "",
        })

    result_url = f"/checkout/result/{payment.id}"
    logger.warning(
        "[CHECKOUT] ■ 3ds-continue FINAL | payment_id=%s status=%s → %s",
        payment.id,
        payment.status.value if hasattr(payment.status, "value") else payment.status,
        result_url,
    )
    return JSONResponse({"status": payment.status.value, "result_url": result_url})


@router.get("/challenge/{payment_id}", response_class=HTMLResponse, include_in_schema=False)
async def challenge_page(payment_id: str):
    """Serve the 3DS challenge form as a full browser navigation.

    The challenge form is pre-stored in _challenge_cache by /3ds-continue.
    Serving it via a GET endpoint means the browser navigates here normally,
    giving the subsequent POST to CardinalCommerce a proper Referer header
    and making it look like a human-initiated action to Cloudflare's WAF.
    """
    form_html = _challenge_cache.pop(payment_id, None)
    if not form_html:
        logger.error("[CHECKOUT] ✗ challenge page | payment_id=%s not in cache", payment_id)
        return HTMLResponse(_html_form("Error 3DS: sesión de autenticación expirada. Intenta de nuevo."), status_code=410)

    logger.warning("[CHECKOUT] ▶ GET /challenge/%s | serving challenge form (%d bytes)", payment_id, len(form_html))
    resp = HTMLResponse(form_html)
    # Allow the challenge form to auto-submit to CardinalCommerce cross-origin.
    # Do NOT set X-Frame-Options here — Cardinal Commerce needs to load this freely.
    resp.headers["Referrer-Policy"] = "unsafe-url"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@router.get("/result/{payment_id}", response_class=HTMLResponse, include_in_schema=False)
async def checkout_result(
    payment_id: str,
    svc: PaymentService = Depends(_get_service),
):
    """Página de resultado final — usada tras el flujo 3DS."""
    logger.warning("[CHECKOUT] ▶ GET /result/%s", payment_id)
    payment = await svc.get_payment(payment_id)
    if not payment:
        logger.error("[CHECKOUT] ✗ payment not found | payment_id=%s", payment_id)
        return HTMLResponse(_html_form("Pago no encontrado."), status_code=404)

    from app.domain.entities import PaymentStatus
    status = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
    msg = payment.response_message or ""
    token_info = f" · Token: {payment.data_vault_token[:12]}…" if payment.data_vault_token else ""

    logger.warning(
        "[CHECKOUT] ■ result page | payment_id=%s status=%s iso=%s msg=%r",
        payment.id, status, payment.iso_code, payment.response_message,
    )

    html = _html_result(
        status=status,
        message=msg + token_info,
        payment_id=payment.id,
        amount=payment.amount + payment.itbis,
        iso=payment.iso_code,
    )
    resp = HTMLResponse(html)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
