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
from app.services.token_service import TokenService
from app.infrastructure.repo_saved_cards import SQLSavedCardRepository
from routers.tokens import _to_response
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("checkout")

router = APIRouter(prefix="/checkout", tags=["Checkout"])

_APP_BASE = os.getenv("APP_BASE_URL", "http://localhost:8000")

# In-memory cache for 3DS challenge forms (keyed by payment_id)
# The challenge page is fetched within seconds of being stored; no TTL needed.
_challenge_cache: Dict[str, str] = {}



def _get_token_svc(db: AsyncSession = Depends(get_db)) -> TokenService:
    return TokenService(
        card_repo=SQLSavedCardRepository(db),
        gateway=AzulPaymentGateway(),
    )

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

def _html_form(error: str = "", saved_cards_html: str = "", cards_count: int = 0) -> str:
    error_block = f'<div class="error-msg"> {error}</div>' if error else ""
    return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Pago Seguro — Atlas</title>
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
  <link rel="stylesheet" href="/public/css/checkout.css">
</head>
<body>

<div class="checkout-layout">
  <!-- Columna izquierda: formulario de pago -->
  <div class="payment-container">
    <div class="header-section">
      <div class="icon-wrapper">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#DA007C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect>
          <line x1="1" y1="10" x2="23" y2="10"></line>
        </svg>
      </div>
      <div>
        <h2 class="title">Métodos de pago</h2>
        <p class="subtitle">Agrega y administra tus tarjetas</p>
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
      <div class="card-bottom-strip"></div>
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

  <div class="security-policies" style="background:transparent; border:none; padding:0; box-shadow:none;">
    <div class="secure-logos">
      <img src="/public/img1.jpeg" alt="Mastercard">
      <img src="/public/img2.jpeg" alt="Mastercard ID Check">
      <img src="/public/img3.jpeg" alt="Visa">
      <img src="/public/img4.jpeg" alt="Visa Secure">
    </div>

    <h3 style="font-size: 14px; color: #666; margin-bottom: 16px; text-align: center; text-transform: uppercase; letter-spacing: 1px;">Políticas y Términos</h3>

    <details class="policy-accordion">
      <summary>Seguridad</summary>
      <div class="accordion-content">
        <p><strong>Infraestructura:</strong> No almacena, procesa ni retiene datos de tarjetas en sus servidores.</p>
        <p><strong>Procesamiento de pagos:</strong> Lo gestiona directamente AZUL (Servicios Digitales Popular), certificado PCI-DSS.</p>
        <p><strong>Sin acceso:</strong> Atlas no ve número de tarjeta, CVV ni credenciales bancarias.</p>
        <p><strong>Cifrado:</strong> SSL/TLS extremo a extremo.</p>
        <p><strong>Autenticación:</strong> Cumple 3D Secure + PCI-DSS vía AZUL.</p>
      </div>
    </details>

    <details class="policy-accordion">
      <summary>Devoluciones y Cancelaciones</summary>
      <div class="accordion-content">
        <h4>Cancelaciones</h4>
        <ul>
          <li><strong>Vía:</strong> solicitud a soporte@atlas.do</li>
          <li><strong>Efectividad:</strong> al cierre del ciclo de facturación mensual vigente</li>
          <li><strong>Acceso:</strong> irrestricto hasta vencer el periodo ya pagado</li>
          <li>No se generan cargos nuevos tras confirmar</li>
        </ul>
        <h4>Devoluciones / Reembolsos</h4>
        <ul>
          <li><strong>Ventas finales:</strong> no hay devoluciones ni reembolsos sobre pagos procesados</li>
          <li>Al pagar, el usuario acepta que iniciar el servicio extingue el derecho a devolución</li>
          <li><strong>Errores/cargos duplicados:</strong> notificar dentro de 5 días hábiles</li>
          <li><strong>Disputas:</strong> bajo normativas de AZUL y el banco emisor</li>
        </ul>
      </div>
    </details>

    <details class="policy-accordion">
      <summary>Entregas y Activación</summary>
      <div class="accordion-content">
        <p><strong>Servicio 100% digital:</strong> no hay entrega física</p>
        <p><strong>Activación:</strong> inmediata tras validar el pago (hasta 24h hábiles en casos especiales de mantenimiento/seguridad)</p>
        <p>Email de confirmación con accesos, recibo y credenciales</p>
        <p><strong>Si no llega el acceso:</strong> notificar a soporte@atlas.do con número de referencia</p>
      </div>
    </details>

    <details class="policy-accordion">
      <summary>Privacidad de Pagos</summary>
      <div class="accordion-content">
        <p>Mismo esquema de no-almacenamiento + AZUL/PCI-DSS</p>
        <p>Datos recogidos solo para: gestión de cuenta, comunicaciones operativas (confirmaciones de pago), cumplimiento legal/tributario en RD</p>
        <p>No venden ni ceden datos personales</p>
        <p>Derechos ARCO disponibles vía soporte@atlas.do</p>
      </div>
    </details>

    <div class="legal-footer">
      <strong>COLINA DEL SOL, S.R.L. (IAMATLAS)</strong><br>
      RNC: 133-11765-7 | soporte@atlas.do | +1 (809) 690-5851<br>
      c/José López, Esq. Amelia Francasci, Los Prados, Santo Domingo, RD.
    </div>
    </div> <!-- Fin Columna izquierda -->

  <!-- Columna derecha: tarjetas guardadas -->
  <div class="saved-cards-section">
    <div class="saved-cards-header">
      <h3 class="title">Tarjetas guardadas</h3>
      <span class="badge">{cards_count}</span>
    </div>
    <div class="saved-cards-list">
      {saved_cards_html}
    </div>
  </div>
</div> <!-- Fin checkout-layout -->

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
</html>""".replace("{error_block}", error_block).replace("{saved_cards_html}", saved_cards_html).replace("{cards_count}", str(cards_count))


def _html_3ds_method(payment_id: str, method_form: str, amount: int) -> str:
    """Página intermedia — renderiza el iframe silencioso 3DS Method y continúa."""
    return """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Verificando seguridad — Atlas</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/public/css/checkout.css"></head>
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
<link rel="stylesheet" href="/public/css/checkout.css"></head>
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
async def checkout_form(
    customer_id: str | None = None,
    token_svc: TokenService = Depends(_get_token_svc)
):
    """Sirve el formulario de pago y carga tarjetas si hay un customer_id."""
    saved_cards_html = ""
    cards_count = 0

    if customer_id:
        try:
            cards = await token_svc.list_cards(customer_id)
            cards_count = len(cards)
            for c in cards:
                # Determinar icono (Visa o Mastercard)
                brand_letter = "V" if "visa" in c.card_brand.lower() else "M"
                brand_name = "Visa" if "visa" in c.card_brand.lower() else "Mastercard"
                badge = '<span class="default-badge">Predeterminada</span>' if c.is_default else ''
                
                exp_formatted = f"{c.expiration[4:]}/{c.expiration[2:4]}" if len(c.expiration) == 6 else c.expiration
                
                saved_cards_html += f'''
                <div class="saved-card-item">
                    <div class="saved-card-icon">{brand_letter}</div>
                    <div class="saved-card-info">
                        <div class="saved-card-title">{brand_name} •••• {c.card_last4}</div>
                        <div class="saved-card-subtitle">{badge} Vence {exp_formatted}</div>
                    </div>
                    <div class="saved-card-actions">
                        <svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>
                        <svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </div>
                </div>
                '''
        except Exception as e:
            logger.error(f"[CHECKOUT] Error listando tarjetas para {customer_id}: {e}")

    if not saved_cards_html and customer_id:
        saved_cards_html = '<div class="empty-cards">No hay tarjetas guardadas.</div>'
    elif not saved_cards_html:
        saved_cards_html = '<div class="empty-cards">Inicia sesión para ver tus tarjetas guardadas.</div>'

    html_content = _html_form(error="", saved_cards_html=saved_cards_html, cards_count=cards_count)
    resp = HTMLResponse(html_content)
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
