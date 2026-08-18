"""
Checkout — Payment Form UI.

GET  /checkout          → Formulario HTML de pago
POST /checkout/process  → Procesa el pago y redirige al resultado

OPCIONAL: Usa cookie ``user_info`` (JWT del auth service) para pre-llenar datos
y mostrar tarjetas guardadas. Fallback a ``access_token`` (legacy).
Si ninguna está presente, muestra checkout como invitado.
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

from fastapi import APIRouter, Depends, Form, Request, Cookie
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

# ── Membership pricing (centavos) ────────────────────────────────────────────
# Centralizado para evitar inconsistencias entre /process y /pay-with-token.
MEMBERSHIP_AMOUNT = int(os.getenv("MEMBERSHIP_AMOUNT", "50000"))   # RD$500.00
MEMBERSHIP_ITBIS  = int(os.getenv("MEMBERSHIP_ITBIS", "9000"))     # RD$90.00

# In-memory cache for 3DS challenge forms (keyed by payment_id)
# The challenge page is fetched within seconds of being stored; no TTL needed.
_challenge_cache: Dict[str, str] = {}


def _resolve_theme(request: Request) -> str:
    """Read and validate the cross-domain 'theme' cookie from iamatlas.do.

    The frontend (ThemeContext.tsx / root.tsx) sets this cookie on .iamatlas.do
    with max-age=31536000 (1 year). It is shared across subdomains so the
    backend can render server-side templates with the correct styling.
    """
    raw = request.cookies.get("theme", "light").lower().strip().strip('"').strip("'")
    return raw if raw in ("dark", "light") else "light"



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

def _html_form(error: str = "", saved_cards_html: str = "", cards_count: int = 0, theme: str = "dark", customer_id: str = "", prefill_email: str = "", prefill_name: str = "", csrf_token: str = "", subscription_status_html: str = "") -> str:
    error_block = f'<div class="error-msg"> {error}</div>' if error else ""
    theme_class = "theme-dark" if theme == "dark" else "theme-light"
    return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Pago Seguro — Atlas</title>
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
  <link rel="stylesheet" href="/public/css/checkout.css?v=3">
  <style>
    .sub-status-banner {
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 24px;
        display: flex;
        align-items: flex-start;
        gap: 12px;
    }
    .theme-light .sub-status-banner {
        background: #f8f9fa;
        border-color: #e9ecef;
    }
    .sub-status-icon {
        flex-shrink: 0;
        margin-top: 2px;
    }
    .sub-status-text {
        flex-grow: 1;
    }
    .sub-status-title {
        font-weight: 600;
        font-size: 14px;
        margin-bottom: 4px;
    }
    .sub-status-desc {
        font-size: 13px;
        color: rgba(255, 255, 255, 0.7);
        line-height: 1.4;
    }
    .theme-light .sub-status-desc { color: #6c757d; }
    
    .status-active .sub-status-icon { color: #10B981; }
    .status-trial .sub-status-icon { color: #3B82F6; }
    .status-paused .sub-status-icon { color: #EF4444; }
    .status-cancelled .sub-status-icon { color: #F59E0B; }
  </style>
</head>
<body class="THEME_CLASS">

<a href="https://www.iamatlas.do/profile" class="back-btn" id="backToProfile" title="Volver al perfil">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <line x1="19" y1="12" x2="5" y2="12"></line>
    <polyline points="12,19 5,12 12,5"></polyline>
  </svg>
  <span>Volver al perfil</span>
</a>

<div class="checkout-layout">
  <!-- Columna izquierda: formulario de pago -->
  <div class="payment-container">
    {subscription_status_html}
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
    <input type="hidden" name="csrf_token" id="csrf_token" value="{csrf_token}"/>
    <input type="hidden" name="customer_id" value="{customer_id}"/>

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
               value="{prefill_name}"
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
               value="{prefill_email}"
               autocomplete="email" required/>
      </div>
    </div>

    <div class="form-group">
      <label>Código de Descuento (opcional)</label>
      <div class="input-with-icon">
        <svg class="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path><line x1="7" y1="7" x2="7.01" y2="7"></line></svg>
        <input type="text" name="promo_code"
               class="signup-input with-icon"
               placeholder="Ej: ESPACIO30"
               maxlength="20" autocomplete="off"/>
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
      <span id="btnText">Pagar RD$590.00</span>
      <div class="spinner" id="spinner"></div>
    </button>
  </form>

    <!-- Fin Columna izquierda -->
  </div>

  <!-- Columna derecha: tarjetas guardadas -->
  <div class="saved-cards-section">
    <div class="saved-cards-header">
      <h3 class="title">Tarjetas guardadas</h3>
      <span class="badge" id="cardsCountBadge">{cards_count}</span>
    </div>
    <div class="saved-cards-grid" id="cardsGrid">
      {saved_cards_html}
    </div>
    <button class="pay-saved-btn" id="paySavedBtn" style="display:none" onclick="payWithSaved()">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line></svg>
      Pagar RD$590.00 con tarjeta seleccionada
    </button>
  </div>
</div> <!-- Fin checkout-layout -->

<div class="section-divider"></div>

<!-- Footer Container -->
<div class="checkout-footer-wrapper">
  <div class="security-policies" style="background:transparent; border:none; padding:0; box-shadow:none;">
    <div class="secure-logos">
      <img src="/public/img1.jpeg" alt="Mastercard">
      <img src="/public/img2.jpeg" alt="Mastercard ID Check">
      <img src="/public/img3.jpeg" alt="Visa">
      <img src="/public/img4.jpeg" alt="Visa Secure">
    </div>

    <h3 style="font-size: 14px; color: #666; margin-bottom: 16px; text-align: center; text-transform: uppercase; letter-spacing: 1px;">Políticas y Términos</h3>

    <div class="policies-grid">
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
    </div>

    <div class="legal-footer">
      <strong>COLINA DEL SOL, S.R.L. (IAMATLAS)</strong><br>
      RNC: 133-11765-7 | soporte@atlas.do | +1 (809) 690-5851<br>
      c/José López, Esq. Amelia Francasci, Los Prados, Santo Domingo, RD.
    </div>
  </div>
</div>
<!-- Modal de confirmación de eliminación -->
<div class="confirm-overlay" id="confirmOverlay" style="display:none" onclick="closeConfirm()">
  <div class="confirm-modal" onclick="event.stopPropagation()">
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#f43f5e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:12px"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>
    <h3>¿Eliminar tarjeta?</h3>
    <p>Esta acción no se puede deshacer. La tarjeta se eliminará de tu cuenta.</p>
    <div class="confirm-actions">
      <button class="confirm-cancel" onclick="closeConfirm()">Cancelar</button>
      <button class="confirm-delete" onclick="doDelete()">Eliminar</button>
    </div>
  </div>
</div>
<div class="feedback-toast" id="feedbackToast"></div>

<script>
(function(){
  'use strict';

  const cardInput = document.getElementById('cardNumber');
  const previewPan = document.getElementById('previewPan');
  const previewName = document.getElementById('previewName');
  const previewExp = document.getElementById('previewExp');

  cardInput.addEventListener('input', function() {
    let v = this.value.replace(/\\D/g,'').slice(0,16);
    this.value = v.match(/.{1,4}/g)?.join(' ') || '';
    const masked = (v + '················').slice(0,16);
    previewPan.textContent = masked.match(/.{1,4}/g).join(' ');
  });

  document.getElementById('cardName').addEventListener('input', function() {
    previewName.textContent = this.value.toUpperCase() || 'NOMBRE DEL TITULAR';
  });

  document.getElementById('cardExp').addEventListener('input', function() {
    let v = this.value.replace(/\\D/g,'');
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

  // --- Tarjetas guardadas: selección, eliminación, pago ---
  var selectedCardId = null;
  var cardToDeleteId = null;

  window.selectCard = function(id) {
    // Deseleccionar todas
    document.querySelectorAll('.saved-visual-card').forEach(function(c) {
      c.classList.remove('selected');
    });
    // Seleccionar la clickeada
    var card = document.querySelector('[data-card-id="' + id + '"]');
    if (card) {
      card.classList.add('selected');
      selectedCardId = id;
      document.getElementById('paySavedBtn').style.display = 'flex';
    }
  };

  window.confirmDeleteCard = function(id) {
    cardToDeleteId = id;
    document.getElementById('confirmOverlay').style.display = 'flex';
  };

  window.closeConfirm = function() {
    cardToDeleteId = null;
    document.getElementById('confirmOverlay').style.display = 'none';
  };

  window.doDelete = function() {
    if (!cardToDeleteId) return;
    var deletingId = cardToDeleteId;
    var card = document.querySelector('[data-card-id="' + deletingId + '"]');
    closeConfirm();

    // Deshabilitar la tarjeta visualmente mientras se procesa
    if (card) {
      card.style.opacity = '0.5';
      card.style.pointerEvents = 'none';
    }

    // Llamar al backend para borrar la tarjeta de la base de datos
    var formData = new FormData();
    formData.append('card_id', deletingId);

    fetch('/checkout/delete-card', {
      method: 'POST',
      body: formData
    }).then(function(resp) {
      if (resp.ok || resp.status === 204) {
        // Éxito: remover la tarjeta del DOM
        if (card) {
          card.style.transition = 'opacity 0.3s, transform 0.3s';
          card.style.opacity = '0';
          card.style.transform = 'scale(0.92)';
          setTimeout(function() { card.remove(); updateCardCount(); }, 300);
        }
        if (selectedCardId === deletingId) {
          selectedCardId = null;
          document.getElementById('paySavedBtn').style.display = 'none';
        }
        showFeedback('success', 'Tarjeta eliminada correctamente.');
      } else {
        // Error del backend: restaurar visual
        if (card) {
          card.style.opacity = '1';
          card.style.pointerEvents = '';
        }
        showFeedback('error', 'No se pudo eliminar la tarjeta. Intenta de nuevo.');
      }
    }).catch(function() {
      // Error de red: restaurar visual
      if (card) {
        card.style.opacity = '1';
        card.style.pointerEvents = '';
      }
      showFeedback('error', 'Error de conexión al eliminar la tarjeta.');
    });
  };

  function updateCardCount() {
    var remaining = document.querySelectorAll('.saved-visual-card').length;
    var badge = document.getElementById('cardsCountBadge');
    if (badge) badge.textContent = remaining;
    if (remaining === 0) {
      var grid = document.getElementById('cardsGrid');
      if (grid) {
        grid.innerHTML = '<div class="empty-cards-state">'
          + '<div class="empty-icon"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line></svg></div>'
          + '<div class="empty-title">No hay tarjetas guardadas</div>'
          + '<div class="empty-desc">Agrega una tarjeta usando el formulario para comenzar a realizar pagos rápidos.</div>'
          + '</div>';
      }
      document.getElementById('paySavedBtn').style.display = 'none';
    }
  }

  window.showFeedback = function(type, msg) {
    var toast = document.getElementById('feedbackToast');
    if (!toast) return;
    toast.textContent = msg;
    toast.className = 'feedback-toast ' + type + ' show';
    setTimeout(function() { toast.classList.remove('show'); }, 3000);
  };

  window.payWithSaved = function() {
    if (!selectedCardId) return;
    var card = document.querySelector('[data-card-id="' + selectedCardId + '"]');
    if (card) {
      showFeedback('success', 'Procesando pago con tarjeta guardada…');
      setTimeout(function() {
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = '/checkout/pay-with-token';
        
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'card_id';
        input.value = selectedCardId;
        form.appendChild(input);

        var csrfEl = document.getElementById('csrf_token');
        var csrf = document.createElement('input');
        csrf.type = 'hidden';
        csrf.name = 'csrf_token';
        csrf.value = csrfEl ? csrfEl.value : '';
        form.appendChild(csrf);

        document.body.appendChild(form);
        form.submit();
      }, 500);
    } else {
      showFeedback('error', 'No se encontró la tarjeta seleccionada.');
    }
  };

  // Auto-select the default card on load
  var defaultCard = document.querySelector('.saved-visual-card[data-default="true"]');
  if (defaultCard) {
    selectCard(defaultCard.getAttribute('data-card-id'));
  }
})();
</script>
</body>
</html>    """.replace("THEME_CLASS", theme_class).replace("{error_block}", error_block).replace("{subscription_status_html}", subscription_status_html).replace("{saved_cards_html}", saved_cards_html).replace("{cards_count}", str(cards_count)).replace("{prefill_email}", prefill_email).replace("{prefill_name}", prefill_name).replace("{customer_id}", customer_id).replace("{csrf_token}", csrf_token)


def _html_3ds_method(payment_id: str, method_form: str, amount: int, theme: str = "light") -> str:
    """Página intermedia — renderiza el iframe silencioso 3DS Method y continúa."""
    theme_class = "theme-dark" if theme == "dark" else "theme-light"
    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Verificando seguridad — Atlas</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/public/css/checkout.css"></head>
<body class="{theme_class}"><div class="card">
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


def _html_result(
    status: str, message: str, payment_id: str, amount: int, iso: str,
    theme: str = "dark", card_last4: str = "", cardholder_name: str = "",
    cardholder_email: str = "", card_saved: bool = False,
) -> str:
    ok = status == "APPROVED"

    # Human-readable decline reasons
    decline_messages = {
        "INSUF FONDOS": "Tu tarjeta no tiene fondos suficientes para esta transacción.",
        "DECLINE": "La transacción fue rechazada por tu banco emisor.",
        "TARJETA INVALIDA": "El número de tarjeta ingresado no es válido.",
        "EXPIRED CARD": "Tu tarjeta ha expirado. Por favor usa otra tarjeta.",
        "FRAUDE": "La transacción fue bloqueada por seguridad.",
        "DO NOT HONOR": "Tu banco rechazó la transacción. Contacta a tu banco para más detalles.",
        "EXCEEDS LIMIT": "El monto excede el límite permitido por tu tarjeta.",
        "INVALID CVV2": "El código de seguridad (CVV) es incorrecto.",
        "RESTRICTED": "Tu tarjeta tiene restricciones para este tipo de transacción.",
    }

    if ok:
        status_title = "¡Pago exitoso!"
        status_subtitle = "Tu pago ha sido procesado correctamente."
        accent = "#10b981"
        accent_light = "rgba(16, 185, 129, 0.08)"
        accent_border = "rgba(16, 185, 129, 0.2)"
        icon_svg = '''<svg width="56" height="56" viewBox="0 0 56 56" fill="none">
            <circle cx="28" cy="28" r="26" stroke="#10b981" stroke-width="2.5" fill="rgba(16,185,129,0.06)"/>
            <path d="M18 29 L24 35 L38 21" stroke="#10b981" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="check-anim"/>
        </svg>'''
    else:
        raw_msg = message.strip().upper()
        status_title = "Pago no procesado"
        status_subtitle = decline_messages.get(raw_msg, "La transacción no pudo ser completada. Intenta nuevamente o usa otra tarjeta.")
        accent = "#e11d48"
        accent_light = "rgba(225, 29, 72, 0.06)"
        accent_border = "rgba(225, 29, 72, 0.15)"
        icon_svg = '''<svg width="56" height="56" viewBox="0 0 56 56" fill="none">
            <circle cx="28" cy="28" r="26" stroke="#e11d48" stroke-width="2.5" fill="rgba(225,29,72,0.04)"/>
            <path d="M21 21 L35 35 M35 21 L21 35" stroke="#e11d48" stroke-width="3" stroke-linecap="round" class="x-anim"/>
        </svg>'''

    amount_display = f"RD${amount / 100:,.2f}"
    card_display = f"•••• {card_last4}" if card_last4 else ""
    ref_short = payment_id[:8].upper()
    theme_class = "theme-dark" if theme == "dark" else "theme-light"

    card_saved_badge = ""
    if ok and card_saved:
        card_saved_badge = '''
        <div class="result-saved-badge">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 6L9 17l-5-5"/>
            </svg>
            <span>Tarjeta guardada para futuros pagos</span>
        </div>'''

    email_line = ""
    if ok and cardholder_email:
        email_line = f'''
        <div class="result-email-notice">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>
                <polyline points="22,6 12,13 2,6"/>
            </svg>
            <span>Recibo enviado a <strong>{cardholder_email}</strong></span>
        </div>'''

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{"Pago Exitoso" if ok else "Pago Rechazado"} — Atlas</title>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/public/css/checkout.css?v=3">
<style>
  /* ── Result page specific styles ─────────────────────────────────────── */
  body {{
    justify-content: center;
    align-items: center;
    min-height: 100vh;
  }}

  .result-wrapper {{
    width: 100%;
    max-width: 460px;
    margin: 0 auto;
    animation: resultFadeUp 0.5s ease-out;
  }}

  @keyframes resultFadeUp {{
    from {{ opacity: 0; transform: translateY(20px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}

  .result-card {{
    background: white;
    border-radius: 16px;
    padding: 40px 32px 32px;
    text-align: center;
    box-shadow: 0 4px 20px rgba(0,0,0,0.05);
    color: #1a1a1a;
  }}

  /* ── Status icon ── */
  .result-icon-wrap {{
    width: 80px; height: 80px;
    border-radius: 50%;
    background: {accent_light};
    border: 1.5px solid {accent_border};
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 0 auto 24px;
  }}
  .check-anim {{
    stroke-dasharray: 32;
    stroke-dashoffset: 32;
    animation: draw 0.5s 0.3s ease-out forwards;
  }}
  .x-anim {{
    stroke-dasharray: 28;
    stroke-dashoffset: 28;
    animation: draw 0.4s 0.3s ease-out forwards;
  }}
  @keyframes draw {{ to {{ stroke-dashoffset: 0; }} }}

  .result-status-title {{
    font-size: 1.4rem;
    font-weight: 700;
    color: {accent};
    margin-bottom: 8px;
    letter-spacing: -0.02em;
  }}
  .result-status-subtitle {{
    font-size: 0.85rem;
    color: #666;
    line-height: 1.6;
    margin-bottom: 28px;
    max-width: 320px;
    margin-left: auto;
    margin-right: auto;
  }}

  /* ── Receipt rows ── */
  .result-divider {{
    height: 1px;
    background: rgba(0,0,0,0.08);
    margin: 0 -32px 16px;
  }}
  .result-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 0;
  }}
  .result-row + .result-row {{
    border-top: 1px solid rgba(0,0,0,0.04);
  }}
  .result-label {{
    font-size: 0.8rem;
    color: #94a3b8;
    font-weight: 500;
  }}
  .result-value {{
    font-size: 0.85rem;
    color: #334155;
    font-weight: 600;
  }}
  .result-value.amount {{
    font-size: 1rem;
    color: #1a1a1a;
    font-weight: 700;
  }}
  .result-value.status-ok {{ color: #10b981; }}
  .result-value.status-fail {{ color: #e11d48; }}

  /* ── Badges ── */
  .result-saved-badge {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 18px;
    padding: 10px 16px;
    background: rgba(16, 185, 129, 0.06);
    border: 1px solid rgba(16, 185, 129, 0.15);
    border-radius: 10px;
    font-size: 0.78rem;
    color: #059669;
    font-weight: 500;
  }}
  .result-email-notice {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    margin-top: 10px;
    font-size: 0.75rem;
    color: #94a3b8;
  }}
  .result-email-notice strong {{ color: #64748b; }}

  /* ── Buttons (Atlas style) ── */
  .result-actions {{ margin-top: 28px; display: flex; flex-direction: column; gap: 10px; }}

  .result-btn-solid {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 100%;
    padding: 14px 20px;
    border: none;
    border-radius: 100px;
    font-family: inherit;
    font-size: 0.88rem;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.2s ease;
    background: #DA007C;
    color: white;
  }}
  .result-btn-solid:hover {{
    background: #c0006c;
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(218, 0, 124, 0.2);
  }}

  .result-btn-outline {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 100%;
    padding: 13px 20px;
    border: 1.5px solid #DA007C;
    border-radius: 100px;
    font-family: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.2s ease;
    background: transparent;
    color: #DA007C;
  }}
  .result-btn-outline:hover {{
    background: rgba(218, 0, 124, 0.04);
    transform: translateY(-1px);
  }}

  .result-footer-ref {{
    margin-top: 20px;
    font-size: 0.7rem;
    color: #cbd5e1;
    text-align: center;
    letter-spacing: 0.02em;
  }}

  /* ── Dark Mode overrides ── */
  body.theme-dark .result-card {{
    background: #1e1e1e;
    color: #e2e8f0;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  }}
  body.theme-dark .result-status-subtitle {{
    color: #94a3b8;
  }}
  body.theme-dark .result-divider {{
    background: rgba(255,255,255,0.08);
  }}
  body.theme-dark .result-row + .result-row {{
    border-top-color: rgba(255, 255, 255, 0.06);
  }}
  body.theme-dark .result-label {{ color: #64748b; }}
  body.theme-dark .result-value {{ color: #cbd5e1; }}
  body.theme-dark .result-value.amount {{ color: #f1f5f9; }}
  body.theme-dark .result-saved-badge {{
    color: #86efac;
    background: rgba(16, 185, 129, 0.08);
    border-color: rgba(16, 185, 129, 0.2);
  }}
  body.theme-dark .result-email-notice {{ color: #64748b; }}
  body.theme-dark .result-email-notice strong {{ color: #94a3b8; }}
  body.theme-dark .result-btn-solid {{
    box-shadow: 0 4px 16px rgba(218, 0, 124, 0.25);
  }}
  body.theme-dark .result-btn-outline {{
    border-color: rgba(218, 0, 124, 0.6);
    color: #ff69b4;
  }}
  body.theme-dark .result-btn-outline:hover {{
    background: rgba(218, 0, 124, 0.08);
  }}
  body.theme-dark .result-footer-ref {{ color: #475569; }}

  @media (max-width: 480px) {{
    .result-card {{ padding: 32px 20px 24px; border-radius: 12px; }}
    .result-wrapper {{ padding: 0 4px; }}
  }}
</style>
</head>
<body class="{theme_class}">

<a href="https://www.iamatlas.do/profile" class="back-btn" id="backToProfile" title="Volver al perfil">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <line x1="19" y1="12" x2="5" y2="12"></line>
    <polyline points="12,19 5,12 12,5"></polyline>
  </svg>
  <span>Volver al perfil</span>
</a>

<div class="result-wrapper">
  <div class="result-card">
    <div class="result-icon-wrap">
      {icon_svg}
    </div>

    <div class="result-status-title">{status_title}</div>
    <div class="result-status-subtitle">{status_subtitle}</div>

    <div class="result-divider"></div>

    <div class="result-row">
      <span class="result-label">Monto</span>
      <span class="result-value amount">{amount_display}</span>
    </div>
    {"<div class='result-row'><span class='result-label'>Tarjeta</span><span class='result-value'>" + card_display + "</span></div>" if card_display else ""}
    {"<div class='result-row'><span class='result-label'>Titular</span><span class='result-value'>" + cardholder_name + "</span></div>" if cardholder_name else ""}
    <div class="result-row">
      <span class="result-label">Estado</span>
      <span class="result-value {"status-ok" if ok else "status-fail"}">{"Aprobado" if ok else "Rechazado"}</span>
    </div>

    {card_saved_badge}
    {email_line}

    <div class="result-actions">
      <a href="https://www.iamatlas.do/profile" class="result-btn-solid">
        {"Ir a mi perfil" if ok else "Intentar de nuevo"}
      </a>
      {"" if ok else '<a href="/checkout" class="result-btn-outline">Usar otra tarjeta</a>'}
    </div>

    <div class="result-footer-ref">Ref: {ref_short} · Atlas Payments</div>
  </div>
</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Trial result page (new users — tokenize only, no charge)
# ---------------------------------------------------------------------------

def _html_result_trial(
    card_last4: str = "",
    cardholder_name: str = "",
    cardholder_email: str = "",
    trial_ends_at: str = "",
    theme: str = "dark",
) -> str:
    """Página de resultado para usuarios nuevos que solo tokenizaron (sin cobro)."""
    theme_class = "theme-dark" if theme == "dark" else "theme-light"
    card_display = f"•••• {card_last4}" if card_last4 else ""

    # Parse trial end date for human display
    trial_date_display = ""
    if trial_ends_at:
        try:
            from datetime import datetime as _dt
            if isinstance(trial_ends_at, str):
                dt = _dt.fromisoformat(trial_ends_at)
            else:
                dt = trial_ends_at
            # Format: "13 de agosto, 2026"
            meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                     "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
            trial_date_display = f"{dt.day} de {meses[dt.month - 1]}, {dt.year}"
        except Exception:
            trial_date_display = f"{trial_ends_at}"[:10]

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Tarjeta Guardada — Atlas</title>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com;">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/public/css/checkout.css?v=3">
<style>
  body {{
    justify-content: center;
    align-items: center;
    min-height: 100vh;
  }}
  .result-wrapper {{
    width: 100%;
    max-width: 460px;
    margin: 0 auto;
    animation: resultFadeUp 0.5s ease-out;
  }}
  @keyframes resultFadeUp {{
    from {{ opacity: 0; transform: translateY(20px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .result-card {{
    background: white;
    border-radius: 16px;
    padding: 40px 32px 32px;
    text-align: center;
    box-shadow: 0 4px 20px rgba(0,0,0,0.05);
    color: #1a1a1a;
  }}
  .result-icon-wrap {{
    width: 80px; height: 80px;
    border-radius: 50%;
    background: rgba(16, 185, 129, 0.08);
    border: 1.5px solid rgba(16, 185, 129, 0.2);
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 0 auto 24px;
  }}
  .check-anim {{
    stroke-dasharray: 32;
    stroke-dashoffset: 32;
    animation: draw 0.5s 0.3s ease-out forwards;
  }}
  @keyframes draw {{ to {{ stroke-dashoffset: 0; }} }}
  .result-status-title {{
    font-size: 1.4rem;
    font-weight: 700;
    color: #10b981;
    margin-bottom: 8px;
    letter-spacing: -0.02em;
  }}
  .result-status-subtitle {{
    font-size: 0.85rem;
    color: #666;
    line-height: 1.6;
    margin-bottom: 28px;
    max-width: 340px;
    margin-left: auto;
    margin-right: auto;
  }}
  .result-divider {{
    height: 1px;
    background: rgba(0,0,0,0.08);
    margin: 0 -32px 16px;
  }}
  .result-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 9px 0;
  }}
  .result-row + .result-row {{
    border-top: 1px solid rgba(0,0,0,0.04);
  }}
  .result-label {{
    font-size: 0.8rem;
    color: #94a3b8;
    font-weight: 500;
  }}
  .result-value {{
    font-size: 0.85rem;
    color: #334155;
    font-weight: 600;
  }}
  .trial-badge {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 18px;
    padding: 12px 16px;
    background: rgba(99, 102, 241, 0.06);
    border: 1px solid rgba(99, 102, 241, 0.15);
    border-radius: 10px;
    font-size: 0.8rem;
    color: #6366f1;
    font-weight: 500;
  }}
  .result-saved-badge {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 10px;
    padding: 10px 16px;
    background: rgba(16, 185, 129, 0.06);
    border: 1px solid rgba(16, 185, 129, 0.15);
    border-radius: 10px;
    font-size: 0.78rem;
    color: #059669;
    font-weight: 500;
  }}
  .result-actions {{ margin-top: 28px; display: flex; flex-direction: column; gap: 10px; }}
  .result-btn-solid {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 100%;
    padding: 14px 20px;
    border: none;
    border-radius: 100px;
    font-family: inherit;
    font-size: 0.88rem;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.2s ease;
    background: #DA007C;
    color: white;
  }}
  .result-btn-solid:hover {{
    background: #c0006c;
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(218, 0, 124, 0.2);
  }}
  .result-footer-ref {{
    margin-top: 20px;
    font-size: 0.7rem;
    color: #cbd5e1;
    text-align: center;
    letter-spacing: 0.02em;
  }}
  /* Dark mode */
  body.theme-dark .result-card {{
    background: #1e1e1e;
    color: #e2e8f0;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  }}
  body.theme-dark .result-status-subtitle {{ color: #94a3b8; }}
  body.theme-dark .result-divider {{ background: rgba(255,255,255,0.08); }}
  body.theme-dark .result-row + .result-row {{ border-top-color: rgba(255,255,255,0.06); }}
  body.theme-dark .result-label {{ color: #64748b; }}
  body.theme-dark .result-value {{ color: #cbd5e1; }}
  body.theme-dark .trial-badge {{
    color: #a5b4fc;
    background: rgba(99, 102, 241, 0.08);
    border-color: rgba(99, 102, 241, 0.2);
  }}
  body.theme-dark .result-saved-badge {{
    color: #86efac;
    background: rgba(16, 185, 129, 0.08);
    border-color: rgba(16, 185, 129, 0.2);
  }}
  body.theme-dark .result-footer-ref {{ color: #475569; }}
  @media (max-width: 480px) {{
    .result-card {{ padding: 32px 20px 24px; border-radius: 12px; }}
    .result-wrapper {{ padding: 0 4px; }}
  }}
</style>
</head>
<body class="{theme_class}">

<a href="https://www.iamatlas.do/profile" class="back-btn" id="backToProfile" title="Volver al perfil">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <line x1="19" y1="12" x2="5" y2="12"></line>
    <polyline points="12,19 5,12 12,5"></polyline>
  </svg>
  <span>Volver al perfil</span>
</a>

<div class="result-wrapper">
  <div class="result-card">
    <div class="result-icon-wrap">
      <svg width="56" height="56" viewBox="0 0 56 56" fill="none">
        <circle cx="28" cy="28" r="26" stroke="#10b981" stroke-width="2.5" fill="rgba(16,185,129,0.06)"/>
        <path d="M18 29 L24 35 L38 21" stroke="#10b981" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="check-anim"/>
      </svg>
    </div>

    <div class="result-status-title">¡Tarjeta guardada!</div>
    <div class="result-status-subtitle">Tu tarjeta ha sido registrada exitosamente. Tu período de prueba de 7 días ha comenzado — no se realizó ningún cobro.</div>

    <div class="result-divider"></div>

    {"<div class='result-row'><span class='result-label'>Tarjeta</span><span class='result-value'>" + card_display + "</span></div>" if card_display else ""}
    {"<div class='result-row'><span class='result-label'>Titular</span><span class='result-value'>" + cardholder_name + "</span></div>" if cardholder_name else ""}
    <div class="result-row">
      <span class="result-label">Estado</span>
      <span class="result-value" style="color:#10b981">Período de prueba</span>
    </div>
    {"<div class='result-row'><span class='result-label'>Primer cobro</span><span class='result-value'>" + trial_date_display + "</span></div>" if trial_date_display else ""}

    <div class="trial-badge">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <polyline points="12 6 12 12 16 14"></polyline>
      </svg>
      <span>7 días de prueba gratis — primer cobro el {trial_date_display}</span>
    </div>

    <div class="result-saved-badge">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 6L9 17l-5-5"/>
      </svg>
      <span>Tarjeta guardada para pagos futuros</span>
    </div>

    <div class="result-actions">
      <a href="https://www.iamatlas.do/profile" class="result-btn-solid">
        Ir a mi perfil
      </a>
    </div>

    <div class="result-footer-ref">Atlas Payments · Período de prueba</div>
  </div>
</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def checkout_form(
    request: Request,
    theme: str | None = None,
    token_svc: TokenService = Depends(_get_token_svc),
    db: AsyncSession = Depends(get_db)
):
    """Sirve el formulario de pago y carga tarjetas si hay un customer_id.

    Si el usuario tiene la cookie `user_info` (JWT del auth service),
    se extrae su ID y se busca su información en public.users para:
      - Pre-llenar los campos del formulario
      - Cargar tarjetas guardadas sin necesidad de query param

    Fallback: si `user_info` no está presente, intenta `access_token` (legacy).
    """
    saved_cards_html = ""
    cards_count = 0
    prefill_email = ""
    prefill_name = ""
    customer_id = ""
    import secrets
    csrf_token = secrets.token_hex(32)

    # ── Read user_info cookie (preferred) or access_token (legacy fallback) ──
    from app.utils.token_utils import decode_user_info_token
    from sqlalchemy import text
    
    # 1) Preferred: user_info cookie (with scope validation)
    user_info_token = request.cookies.get("user_info")
    user_data = decode_user_info_token(user_info_token, require_scope="user_info")
    cookie_source = "user_info"

    # 2) Legacy fallback: access_token cookie (no scope validation)
    if user_data is None:
        user_info_token = request.cookies.get("access_token")
        user_data = decode_user_info_token(user_info_token)
        cookie_source = "access_token"

    if user_data:
        sub = user_data.get("sub", "")
        prefill_email = user_data.get("email", "")
        name = user_data.get("name", "")
        last_name = user_data.get("last_name", "")
        
        # Buscar en la base de datos si nos falta el correo o el nombre
        if sub and (not prefill_email or not name):
            try:
                result = await db.execute(
                    text("SELECT email, name, last_name FROM public.users WHERE uuid = :sub OR email = :sub LIMIT 1"),
                    {"sub": sub}
                )
                row = result.fetchone()
                if row:
                    prefill_email = row[0] or prefill_email
                    name = row[1] or name
                    last_name = row[2] or last_name
            except Exception as e:
                logger.error("[CHECKOUT] Error querying public.users: %s", e)

        prefill_name = f"{name} {last_name}".strip()
        # Use user UUID (sub) as canonical customer_id for consistency across all pagos tables
        customer_id = sub or prefill_email
        logger.warning(
            "[CHECKOUT] %s cookie decoded | sub=%s email=%s name=%s customer_id=%s",
            cookie_source,
            sub, prefill_email, prefill_name, customer_id,
        )

    subscription_status_html = ""
    if customer_id:
        try:
            from sqlalchemy import select
            from app.infrastructure.models import RecurringPaymentModel
            from datetime import datetime, timezone
            
            now = datetime.now(timezone.utc)
            subs_result = await db.execute(
                select(RecurringPaymentModel)
                .where(RecurringPaymentModel.customer_id == customer_id)
                .order_by(RecurringPaymentModel.created_at.desc())
                .limit(1)
            )
            sub = subs_result.scalar_one_or_none()
            
            if sub:
                status_class = "status-cancelled"
                icon_svg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>'
                title = "Sin membresía activa"
                desc = "Tu suscripción ha sido cancelada. Agrega una nueva tarjeta para reactivarla."
                
                if sub.status == "ACTIVE":
                    if sub.trial_ends_at and sub.trial_ends_at > now:
                        status_class = "status-trial"
                        icon_svg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path></svg>'
                        title = "Prueba gratuita activa"
                        desc = f"Tu primer cobro de RD${sub.amount/100:,.2f} se realizará el {sub.trial_ends_at.strftime('%d/%m/%Y')}."
                    else:
                        status_class = "status-active"
                        icon_svg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>'
                        title = "Membresía activa"
                        desc = f"Tu próximo cobro será el {sub.next_charge_at.strftime('%d/%m/%Y')} si aplica." if sub.next_charge_at else "Tu membresía está activa."
                elif sub.status == "PAUSED":
                    status_class = "status-paused"
                    icon_svg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>'
                    title = "Problema con tu pago"
                    desc = "No pudimos procesar tu último cobro. Por favor, actualiza tu tarjeta para evitar la interrupción del servicio."
                
                subscription_status_html = f'''
                <div class="sub-status-banner {status_class}">
                    <div class="sub-status-icon">{icon_svg}</div>
                    <div class="sub-status-text">
                        <div class="sub-status-title">{title}</div>
                        <div class="sub-status-desc">{desc}</div>
                    </div>
                </div>
                '''
        except Exception as e:
            logger.error(f"[CHECKOUT] Error fetching subscription status: {e}")

        try:
            cards = await token_svc.list_cards(customer_id)
            cards_count = len(cards)
            for c in cards:
                # Determinar icono y nombre de marca
                is_visa = "visa" in c.card_brand.lower()
                brand_name = "Visa" if is_visa else "Mastercard"
                # SVG logo para cada marca
                if is_visa:
                    brand_svg = '<svg width="32" height="20" viewBox="0 0 48 16" fill="white"><text x="0" y="13" font-family="Arial,sans-serif" font-weight="700" font-size="14" letter-spacing="1">VISA</text></svg>'
                else:
                    brand_svg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="9" cy="12" r="7" fill="#EB001B" opacity="0.8"/><circle cx="15" cy="12" r="7" fill="#F79E1B" opacity="0.8"/></svg>'
                
                exp_formatted = f"{c.expiration[4:]}/{c.expiration[2:4]}" if len(c.expiration) == 6 else c.expiration
                
                default_badge = '<div class="card-default-badge">Predeterminada</div>' if c.is_default else ''
                default_attr = 'true' if c.is_default else 'false'
                card_id = c.id if hasattr(c, 'id') else c.card_last4
                
                saved_cards_html += f'''
                <div class="saved-visual-card" data-card-id="{card_id}" data-default="{default_attr}" onclick="selectCard('{card_id}')">
                    <div class="card-bg-decoration"></div>
                    {default_badge}
                    <div class="svc-brand">
                        {brand_svg}
                        <span class="svc-brand-name">{brand_name}</span>
                    </div>
                    <div class="svc-number">•••• •••• •••• {c.card_last4}</div>
                    <div class="svc-footer">
                        <span class="svc-label">Vence {exp_formatted}</span>
                    </div>
                    <button class="card-delete-btn" onclick="event.stopPropagation(); confirmDeleteCard('{card_id}')" title="Eliminar tarjeta">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                </div>
                '''
        except Exception as e:
            logger.error(f"[CHECKOUT] Error listando tarjetas para {customer_id}: {e}")

    if not saved_cards_html and customer_id:
        saved_cards_html = '''<div class="empty-cards-state">
            <div class="empty-icon"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line></svg></div>
            <div class="empty-title">No hay tarjetas guardadas</div>
            <div class="empty-desc">Agrega una tarjeta usando el formulario para comenzar a realizar pagos rápidos.</div>
        </div>'''
    elif not saved_cards_html:
        saved_cards_html = '''<div class="empty-cards-state">
            <div class="empty-icon"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#aaa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"></rect><line x1="1" y1="10" x2="23" y2="10"></line></svg></div>
            <div class="empty-title">Inicia sesión</div>
            <div class="empty-desc">Ingresa con tu cuenta para ver y administrar tus tarjetas guardadas.</div>
        </div>'''

    # Normalize theme value
    resolved = theme.lower().strip() if theme and theme.lower().strip() in ("dark", "light") else _resolve_theme(request)

    html_content = _html_form(
        error="",
        saved_cards_html=saved_cards_html,
        cards_count=cards_count,
        theme=resolved,
        customer_id=customer_id or "",
        prefill_email=prefill_email,
        prefill_name=prefill_name,
        csrf_token=csrf_token,
        subscription_status_html=subscription_status_html,
    )
    resp = HTMLResponse(html_content)
    resp.set_cookie(
        "checkout_csrf", 
        csrf_token, 
        httponly=True, 
        secure=request.url.scheme == "https", 
        samesite="lax", 
        max_age=3600
    )
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@router.post("/delete-card", include_in_schema=False)
async def checkout_delete_card(
    request: Request,
    card_id: str = Form(...),
    token_svc: TokenService = Depends(_get_token_svc),
):
    """Elimina una tarjeta guardada desde el checkout.

    Protegido por cookie user_info (no requiere X-API-Key).
    Extrae el customer_id del JWT de sesión para evitar manipulación.
    """
    from fastapi.responses import JSONResponse
    from app.utils.token_utils import decode_user_info_token

    # Extraer customer_id del JWT (NO del form, para evitar manipulación)
    user_info_token = request.cookies.get("user_info")
    user_data = decode_user_info_token(user_info_token, require_scope="user_info")
    if user_data is None:
        user_info_token = request.cookies.get("access_token")
        user_data = decode_user_info_token(user_info_token)

    customer_id = ""
    if user_data:
        customer_id = user_data.get("sub", "") or user_data.get("email", "")

    if not customer_id:
        return JSONResponse({"error": "Sesión expirada"}, status_code=401)

    try:
        await token_svc.delete_card_by_id(card_id=card_id, customer_email=customer_id)
        return JSONResponse({"ok": True}, status_code=200)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except Exception as exc:
        logger.error("[CHECKOUT] Error deleting card %s for %s: %s", card_id, customer_id, exc)
        return JSONResponse({"error": "Error interno al eliminar la tarjeta"}, status_code=500)


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
    customer_id: str = Form(""),
    csrf_token: str = Form(""),
    promo_code: str = Form(None),
    checkout_csrf: str = Cookie(None),
    svc: PaymentService = Depends(_get_service),
    token_svc: TokenService = Depends(_get_token_svc),
    db: AsyncSession = Depends(get_db),
):
    """Procesa el pago — recibe el form POST, llama a AZUL, redirige al resultado.

    Flujo diferenciado:
    - Usuario NUEVO (sin suscripciones ni tarjetas previas): solo tokeniza la
      tarjeta (TrxType=CREATE, sin cobro) y crea suscripción con trial de 7 días.
    - Usuario EXISTENTE: cobro inmediato normal (Sale + 3DS).
    """
    theme = _resolve_theme(request)
    import secrets
    if not checkout_csrf or not csrf_token or not secrets.compare_digest(csrf_token, checkout_csrf):
        logger.warning("[CHECKOUT] ✗ Validación CSRF fallida en /checkout/process")
        return HTMLResponse(_html_form("Error de seguridad (CSRF). Intenta de nuevo.", theme=theme), status_code=400)

    # ── Extraer customer_id del JWT (no del form, para evitar manipulación) ──
    from app.utils.token_utils import decode_user_info_token
    user_info_token = request.cookies.get("user_info")
    user_data = decode_user_info_token(user_info_token, require_scope="user_info")
    if user_data is None:
        user_info_token = request.cookies.get("access_token")
        user_data = decode_user_info_token(user_info_token)

    user_phone = ""
    if user_data:
        customer_id = user_data.get("sub", "") or user_data.get("email", "")
        if customer_id:
            try:
                from sqlalchemy import text
                import re
                result = await db.execute(
                    text("SELECT phone FROM public.users WHERE uuid = :sub OR email = :sub LIMIT 1"),
                    {"sub": customer_id}
                )
                row = result.fetchone()
                if row and row[0]:
                    user_phone = re.sub(r'\D', '', row[0])
            except Exception as e:
                logger.error("[CHECKOUT] Error querying public.users for phone: %s", e)

    if not customer_id:
        return HTMLResponse(_html_form("Sesión expirada. Inicia sesión nuevamente.", theme=theme), status_code=401)

    card_clean = card_number.replace(" ", "").strip()
    card_masked = f"{'*' * (len(card_clean) - 4)}{card_clean[-4:]}" if len(card_clean) >= 4 else "****"

    logger.warning(
        "[CHECKOUT] ▶ POST /checkout/process | card=%s exp=%s email=%s customer_id=%s",
        card_masked, expiration, cardholder_email, customer_id,
    )

    # Convertir expiración MM/AA → YYYYMM
    try:
        mm, yy = expiration.strip().split("/")
        exp_azul = f"20{yy.strip()}{mm.strip().zfill(2)}"
    except Exception:
        logger.error("[CHECKOUT] ✗ Expiration parse failed: %r", expiration)
        return HTMLResponse(_html_form("Fecha de vencimiento inválida. Usa MM/AA.", theme=theme), status_code=422)

    # Monto de membresía (centralizado en constantes del módulo)
    amount = MEMBERSHIP_AMOUNT
    itbis  = MEMBERSHIP_ITBIS

    # ── Detectar si es usuario nuevo ──────────────────────────────────────
    is_new_user = False
    if customer_id:
        try:
            from sqlalchemy import select
            from app.infrastructure.models import RecurringPaymentModel, SavedCardModel
            from app.domain.entities import SubscriptionStatus

            prior_subs = await db.execute(
                select(RecurringPaymentModel.id).where(
                    RecurringPaymentModel.customer_id == customer_id,
                    RecurringPaymentModel.status == SubscriptionStatus.ACTIVE.value,
                ).limit(1)
            )
            prior_cards = await db.execute(
                select(SavedCardModel.id).where(
                    SavedCardModel.customer_id == customer_id,
                ).limit(1)
            )
            is_new_user = (
                prior_subs.scalar_one_or_none() is None
                and prior_cards.scalar_one_or_none() is None
            )
            logger.warning(
                "[CHECKOUT] user detection | customer_id=%s is_new_user=%s",
                customer_id, is_new_user,
            )
        except Exception as exc:
            logger.error(
                "[CHECKOUT] ✗ user detection FAILED (defaulting to charge) | err=%s",
                exc,
            )

    # ── Flujo para USUARIO NUEVO: tokenizar sin cobrar ──────────────────────
    # Cadena de fallback: CREATE → Hold+Void → Sale
    # Cada paso intenta la siguiente opción si Azul devuelve VALIDATION_ERROR:TrxType
    payment = None  # Se define aquí para que Hold pueda setearla antes del Sale
    if is_new_user and customer_id:
        logger.warning(
            "[CHECKOUT] → NEW USER — tokenize only (no charge) | customer_id=%s card=%s",
            customer_id, card_masked,
        )

        # ── Paso 1: Intentar CREATE (tokenizar sin cobro, sin 3DS) ────────
        try:
            saved_card = await token_svc.register_card(
                customer_id=customer_id,
                card_number=card_clean,
                expiration=exp_azul,
                cvc=cvc.strip(),
                cardholder_name=cardholder_name.strip(),
                cardholder_email=cardholder_email.strip(),
            )
            logger.warning(
                "[CHECKOUT] ✓ card tokenized (CREATE) | customer_id=%s token=%s last4=%s",
                customer_id,
                saved_card.token[:12] + "…" if saved_card.token else "(none)",
                saved_card.card_last4,
            )
            # CREATE exitoso — crear trial y retornar sin cobro
            trial_result = None
            try:
                from app.services.post_payment import create_trial_subscription
                trial_result = await create_trial_subscription(
                    customer_id=customer_id,
                    saved_card=saved_card,
                    amount=amount,
                    itbis=itbis,
                    cardholder_email=cardholder_email.strip(),
                    db=db,
                    promo_code=promo_code,
                    user_phone=user_phone,
                )
                logger.warning(
                    "[CHECKOUT] ✓ trial subscription created | customer_id=%s trial_ends=%s",
                    customer_id, trial_result.trial_ends_at,
                )
            except Exception as exc:
                logger.error(
                    "[CHECKOUT] ✗ trial subscription FAILED | customer_id=%s err=%s",
                    customer_id, exc,
                )

            html = _html_result_trial(
                card_last4=saved_card.card_last4,
                cardholder_name=cardholder_name.strip(),
                cardholder_email=cardholder_email.strip(),
                trial_ends_at=trial_result.trial_ends_at if trial_result else "",
                theme=theme,
            )
            resp = HTMLResponse(html)
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp

        except Exception as exc:
            err_msg = str(exc)
            if "VALIDATION_ERROR:TrxType" not in err_msg:
                logger.error(
                    "[CHECKOUT] ✗ tokenize EXCEPTION | type=%s msg=%s",
                    type(exc).__name__, err_msg[:400],
                )
                return HTMLResponse(
                    _html_form(f"Error al guardar tarjeta: {exc}", theme=theme, customer_id=customer_id),
                    status_code=422,
                )
            logger.warning(
                "[CHECKOUT] ⚠ CREATE not enabled — trying Hold+Void | customer_id=%s",
                customer_id,
            )

        # ── Paso 2: Intentar Hold+Void (3DS + tokenizar, liberar fondos) ──
        from app.infrastructure.azul_gateway import AzulIntegrationError
        try:
            # Build browser_info for 3DS
            client_ip = (
                request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or (request.client.host if request.client else "")
                or browser_ip
            )
            hold_browser_info = {
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
            payment = await svc.process_hold_verify(
                card_number=card_clean,
                expiration=exp_azul,
                cvc=cvc.strip(),
                order_id=f"HOLD-{uuid.uuid4().hex[:8].upper()}",
                cardholder_name=cardholder_name.strip(),
                cardholder_email=cardholder_email.strip(),
                customer_id=customer_id,
                browser_info=hold_browser_info,
            )
            logger.warning(
                "[CHECKOUT] ← Hold response | payment_id=%s status=%s iso=%s",
                payment.id, payment.status.value, payment.iso_code,
            )
            # Hold va por 3DS — reusar el mismo flujo de abajo (3DS method/challenge/result)
            # El auto-void se ejecuta cuando APPROVED (detectado por order_id "HOLD-*")

        except AzulIntegrationError as exc:
            hold_err = str(exc)
            if "VALIDATION_ERROR:TrxType" in hold_err:
                logger.warning(
                    "[CHECKOUT] ⚠ Hold not enabled either — falling back to Sale | customer_id=%s",
                    customer_id,
                )
                # Fall through to Sale flow below
            else:
                logger.error("[CHECKOUT] ✗ Hold EXCEPTION | %s", hold_err[:400])
                return HTMLResponse(
                    _html_form(f"Error al verificar tarjeta: {exc}", theme=theme, customer_id=customer_id),
                    status_code=422,
                )
        except Exception as exc:
            logger.error("[CHECKOUT] ✗ Hold EXCEPTION | type=%s msg=%s", type(exc).__name__, str(exc)[:400])
            # Fall through to Sale
        else:
            # Hold fue enviado — redirigir al flujo 3DS (method/challenge/result)
            # El código de 3DS de abajo maneja el payment object
            pass  # payment ya está definido, cae al manejo de 3DS abajo

    # ── Flujo Sale (usuario existente O fallback final de usuario nuevo) ───
    if payment is None:
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

        # ── Llamada a Azul (cobro real) ───────────────────────────────────────
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
                customer_id=customer_id,
                browser_info=browser_info,
            )
        except Exception as exc:
            logger.error(
                "[CHECKOUT] ✗ process_sale EXCEPTION | type=%s msg=%s",
                type(exc).__name__, str(exc)[:400],
            )
            return HTMLResponse(_html_form(f"Error al procesar: {exc}", theme=theme), status_code=422)

    # A partir de aquí, payment está definido (por Hold o por Sale)
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
                theme=theme,
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
                return HTMLResponse(_html_form(f"Error 3DS (método): {exc}", theme=theme), status_code=502)
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
            # Store in Redis (survives deploys) + in-memory fallback
            _challenge_cache[payment.id] = challenge_form
            try:
                from app.infrastructure.redis_client import store_challenge_form
                await store_challenge_form(payment.id, challenge_form)
            except Exception as e:
                logger.error("[CHECKOUT] Redis store_challenge_form failed: %s", e)
            from fastapi.responses import RedirectResponse
            # Redirect using 303 See Other to turn POST into GET and prevent Safari download bug
            return RedirectResponse(url=f"/checkout/challenge/{payment.id}", status_code=303)
        if redirect_url:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=redirect_url, status_code=303)
        logger.error("[CHECKOUT] ✗ 3DS CHALLENGE | payment_id=%s no challenge_form and no redirect_url", payment.id)
        return HTMLResponse(_html_form("Error 3DS: sin URL de challenge.", theme=theme), status_code=502)

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

    if status == "APPROVED":
        # Auto-void si fue un Hold de verificación (order_id empieza con HOLD-)
        if payment.order_id and payment.order_id.startswith("HOLD-"):
            try:
                from datetime import datetime as _dt, timezone as _tz
                original_date = _dt.now(_tz.utc).strftime("%Y%m%d")
                void_result = await svc._gw.void(
                    azul_order_id=payment.azul_order_id,
                    original_date=original_date,
                )
                logger.warning(
                    "[CHECKOUT] ✓ Hold auto-voided | payment_id=%s azul_order_id=%s",
                    payment.id, payment.azul_order_id,
                )
            except Exception as void_exc:
                logger.error(
                    "[CHECKOUT] ✗ Hold auto-void FAILED | payment_id=%s err=%s",
                    payment.id, void_exc,
                )
            # Crear trial subscription para el usuario verificado
            trial_result = None
            try:
                from app.services.post_payment import create_trial_subscription
                from app.domain.entities import SavedCard as _SC
                # Build a minimal SavedCard for the trial
                _card = _SC(
                    customer_id=customer_id,
                    token=payment.data_vault_token or "",
                    card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                    expiration=exp_azul,
                )
                trial_result = await create_trial_subscription(
                    customer_id=customer_id,
                    saved_card=_card,
                    amount=amount,
                    itbis=itbis,
                    cardholder_email=payment.cardholder_email or "",
                    db=db,
                    promo_code=promo_code,
                    user_phone=user_phone,
                )
                logger.warning(
                    "[CHECKOUT] ✓ trial created (hold-verify) | customer_id=%s",
                    customer_id,
                )
            except Exception as te:
                logger.error("[CHECKOUT] ✗ trial FAILED (hold-verify) | %s", te)

            html = _html_result_trial(
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                cardholder_name=payment.cardholder_name or "",
                cardholder_email=payment.cardholder_email or "",
                trial_ends_at=trial_result.trial_ends_at if trial_result else "",
                theme=theme,
            )
            resp = HTMLResponse(html)
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp

        from app.services.post_payment import handle_post_payment_actions, create_subscription_if_needed
        try:
            await handle_post_payment_actions(payment)
        except Exception as _pp_exc:
            logger.error("[CHECKOUT] ✗ post-payment actions FAILED | payment_id=%s err=%s", payment.id, _pp_exc)
        await create_subscription_if_needed(payment, customer_id, db, card_expiration=exp_azul, promo_code=promo_code, user_phone=user_phone)

    html = _html_result(
        status=status,
        message=msg,
        payment_id=payment.id,
        amount=payment.amount + payment.itbis,
        iso=payment.iso_code,
        theme=theme,
        card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
        cardholder_name=payment.cardholder_name or "",
        cardholder_email=payment.cardholder_email or "",
        card_saved=bool(payment.data_vault_token),
    )
    resp = HTMLResponse(html)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@router.post("/pay-with-token", response_class=HTMLResponse, include_in_schema=False)
async def pay_with_token(
    request: Request,
    card_id: str = Form(...),
    csrf_token: str = Form(""),
    checkout_csrf: str = Cookie(None),
    svc: PaymentService = Depends(_get_service),
    token_svc: TokenService = Depends(_get_token_svc),
    db: AsyncSession = Depends(get_db),
):
    """Procesa un cobro (CIT) usando una tarjeta previamente guardada (DataVault Token)."""
    theme = _resolve_theme(request)
    import secrets
    if not checkout_csrf or not csrf_token or not secrets.compare_digest(csrf_token, checkout_csrf):
        logger.warning("[CHECKOUT] ✗ Validación CSRF fallida en /checkout/pay-with-token")
        return HTMLResponse(_html_form("Error de seguridad (CSRF). Intenta de nuevo.", theme=theme), status_code=400)
    
    from app.utils.token_utils import decode_user_info_token
    user_info_token = request.cookies.get("user_info")
    user_data = decode_user_info_token(user_info_token, require_scope="user_info")
    if user_data is None:
        user_info_token = request.cookies.get("access_token")
        user_data = decode_user_info_token(user_info_token)
    
    customer_id = ""
    if user_data:
        customer_id = user_data.get("sub", "") or user_data.get("email", "")
        
    if not customer_id:
        return HTMLResponse(_html_form("Sesión expirada. Inicia sesión nuevamente.", theme=theme), status_code=401)
        
    logger.warning("[CHECKOUT] ▶ POST /pay-with-token | customer_id=%s card_id=%s", customer_id, card_id)

    cards = await token_svc.list_cards(customer_id)
    selected_card = next((c for c in cards if hasattr(c, 'id') and c.id == card_id), None)
    
    if not selected_card or not hasattr(selected_card, 'token') or not selected_card.token:
        logger.error("[CHECKOUT] ✗ Tarjeta no encontrada o sin token: %s", card_id)
        return HTMLResponse(_html_form("Tarjeta no encontrada o inválida.", theme=theme, customer_id=customer_id), status_code=404)
        
    token = selected_card.token
    expiration = getattr(selected_card, 'expiration', '')

    from sqlalchemy import select
    from app.infrastructure.models import RecurringPaymentModel
    from app.domain.entities import SubscriptionStatus
    
    existing_active = await db.execute(
        select(RecurringPaymentModel).where(
            RecurringPaymentModel.customer_id == customer_id,
            RecurringPaymentModel.status == SubscriptionStatus.ACTIVE.value,
        )
    )
    if existing_active.scalar_one_or_none():
        logger.warning("[CHECKOUT] ⚠ Usuario ya tiene suscripción activa | customer_id=%s", customer_id)
        return HTMLResponse(_html_result("DECLINED", "El usuario ya tiene una suscripción activa.", "", 0, "", theme=theme, card_last4=""), status_code=409)

    amount = MEMBERSHIP_AMOUNT
    itbis = MEMBERSHIP_ITBIS
    
    from app.domain.entities import Payment, PaymentType, PaymentStatus
    from app.infrastructure.azul_gateway import AzulPaymentGateway
    gateway = AzulPaymentGateway()
    
    payment = Payment(
        amount=amount,
        itbis=itbis,
        payment_type=PaymentType.RECURRING,
        auth_mode="splitit",
        cardholder_email=user_data.get("email", "") if user_data else "",
        initiated_by="cardholder",
    )
    
    try:
        payment, txn = await gateway.sale_cit(payment, token)
        payment.data_vault_token = payment.data_vault_token or token
        payment.card_number_masked = payment.card_number_masked or getattr(selected_card, 'card_last4', "")
        
        from app.infrastructure.repo_impl import SQLPaymentRepository, SQLTransactionRepository
        await SQLPaymentRepository(db).save(payment)
        await SQLTransactionRepository(db).save(txn)
    except Exception as e:
        logger.error("[CHECKOUT] ✗ Error llamando a sale_cit: %s", e)
        return HTMLResponse(_html_result("DECLINED", "Ocurrió un error al procesar el pago. Por favor, intenta de nuevo más tarde.", "", 0, "", theme=theme, card_last4=""), status_code=502)

    status = payment.status == PaymentStatus.APPROVED
    msg = payment.response_message or "Declinada"
    
    if status:
        msg = "¡Suscripción exitosa!"
        from app.services.post_payment import handle_post_payment_actions, create_subscription_if_needed
        try:
            await handle_post_payment_actions(payment)
        except Exception as _pp_exc:
            logger.error("[CHECKOUT] post-payment actions FAILED | payment_id=%s err=%s", payment.id, _pp_exc)
        await create_subscription_if_needed(payment, customer_id, db, card_expiration=expiration)

    html = _html_result(
        status="APPROVED" if status else "DECLINED",
        message=msg,
        payment_id=payment.id,
        amount=payment.amount + payment.itbis,
        iso=payment.iso_code,
        theme=theme,
        card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else selected_card.card_last4,
        cardholder_name=payment.cardholder_name or getattr(selected_card, 'cardholder_name', ""),
        cardholder_email=payment.cardholder_email or "",
        card_saved=True,
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
    db: AsyncSession = Depends(get_db),
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
            # Store in Redis (survives deploys) + in-memory fallback
            _challenge_cache[payment.id] = challenge_form
            from app.infrastructure.redis_client import store_challenge_form
            await store_challenge_form(payment.id, challenge_form)
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
    
    if payment.status == PaymentStatus.APPROVED:
        # Auto-void si fue un Hold de verificación
        if payment.order_id and payment.order_id.startswith("HOLD-"):
            try:
                from datetime import datetime as _dt, timezone as _tz
                from app.infrastructure.azul_gateway import AzulPaymentGateway
                _gw = AzulPaymentGateway()
                original_date = _dt.now(_tz.utc).strftime("%Y%m%d")
                await _gw.void(
                    azul_order_id=payment.azul_order_id,
                    original_date=original_date,
                )
                logger.warning(
                    "[CHECKOUT] ✓ Hold auto-voided (3ds-continue) | payment_id=%s",
                    payment.id,
                )
            except Exception as void_exc:
                logger.error(
                    "[CHECKOUT] ✗ Hold auto-void FAILED (3ds-continue) | payment_id=%s err=%s",
                    payment.id, void_exc,
                )

        from app.services.post_payment import handle_post_payment_actions, create_subscription_if_needed
        try:
            await handle_post_payment_actions(payment)
        except Exception as _pp_exc:
            logger.error("[CHECKOUT] post-payment actions FAILED | payment_id=%s err=%s", payment.id, _pp_exc)
        if payment.customer_id:
            exp_db = ""
            if payment.data_vault_token:
                from sqlalchemy import select
                from app.infrastructure.models import SavedCardModel
                card_query = await db.execute(
                    select(SavedCardModel.expiration)
                    .where(SavedCardModel.token == payment.data_vault_token)
                )
                exp_db = card_query.scalar_one_or_none() or ""
            await create_subscription_if_needed(payment, payment.customer_id, db, card_expiration=exp_db)

    return JSONResponse({"status": payment.status.value, "result_url": result_url})


@router.get("/challenge/{payment_id}", response_class=HTMLResponse, include_in_schema=False)
async def challenge_page(
    request: Request,
    payment_id: str,
    svc: PaymentService = Depends(_get_service)
):
    """Serve the 3DS challenge form as a full browser navigation.

    The challenge form is pre-stored in _challenge_cache by /3ds-continue.
    Serving it via a GET endpoint means the browser navigates here normally,
    giving the subsequent POST to CardinalCommerce a proper Referer header
    and making it look like a human-initiated action to Cloudflare's WAF.
    """
    # Try Redis first (survives deploys), then in-memory fallback
    from app.infrastructure.redis_client import get_challenge_form
    form_html = await get_challenge_form(payment_id)
    if not form_html:
        form_html = _challenge_cache.pop(payment_id, None)
    if not form_html:
        logger.warning("[CHECKOUT] challenge page | payment_id=%s not in Redis or memory cache, checking DB", payment_id)
        payment = await svc.get_payment(payment_id)
        if payment and payment.threeds_challenge_form:
            form_html = payment.threeds_challenge_form
        else:
            logger.error("[CHECKOUT] ✗ challenge page | payment_id=%s not in cache nor DB", payment_id)
            theme = _resolve_theme(request)
            return HTMLResponse(_html_form("Error 3DS: sesión de autenticación expirada. Intenta de nuevo.", theme=theme), status_code=410)

    logger.warning("[CHECKOUT] ▶ GET /challenge/%s | serving challenge form (%d bytes)", payment_id, len(form_html))

    # 1. Force explicitly POST method in case Azul omits it (fixes Safari GET bug)
    if 'method="POST"' not in form_html and "method='POST'" not in form_html:
        form_html = form_html.replace("<form ", '<form method="POST" ')

    # 2. Add visual fallback button if Safari blocks the auto-submit JS
    fallback_button = """
    <div style="text-align: center; margin-top: 50px; font-family: sans-serif;">
        <p>Si no eres redirigido automáticamente a tu banco en unos segundos...</p>
        <button onclick="document.forms[0].submit()" style="padding: 10px 20px; background: #DA007C; color: white; border: none; border-radius: 5px; font-size: 16px; cursor: pointer;">
            Haz clic aquí para continuar
        </button>
    </div>
    </body>
    """
    if "</body>" in form_html:
        form_html = form_html.replace("</body>", fallback_button)

    resp = HTMLResponse(form_html)
    # Allow the challenge form to auto-submit to CardinalCommerce cross-origin.
    # Do NOT set X-Frame-Options here — Cardinal Commerce needs to load this freely.
    resp.headers["Referrer-Policy"] = "unsafe-url"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@router.get("/result/{payment_id}", response_class=HTMLResponse, include_in_schema=False)
async def checkout_result(
    request: Request,
    payment_id: str,
    svc: PaymentService = Depends(_get_service),
):
    """Página de resultado final — usada tras el flujo 3DS."""
    theme = _resolve_theme(request)
    logger.warning("[CHECKOUT] ▶ GET /result/%s", payment_id)
    payment = await svc.get_payment(payment_id)
    if not payment:
        logger.error("[CHECKOUT] ✗ payment not found | payment_id=%s", payment_id)
        return HTMLResponse(_html_form("Pago no encontrado.", theme=theme), status_code=404)

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
        message=msg,
        payment_id=payment.id,
        amount=0 if (payment.order_id and payment.order_id.startswith("HOLD-")) else (payment.amount + payment.itbis),
        iso=payment.iso_code,
        theme=theme,
        card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
        cardholder_name=payment.cardholder_name or "",
        cardholder_email=payment.cardholder_email or "",
        card_saved=bool(payment.data_vault_token),
    )
    resp = HTMLResponse(html)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
