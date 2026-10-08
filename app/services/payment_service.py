"""
Payment service — orchestrates single payments and service payments.

Idempotency
-----------
A durable operation is reserved before contacting Azul. General API charges
require an ``idempotency_key``; membership uses a shared customer/cycle key.
Uncertain results block resubmission until reconciliation.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import (
    IsoCode,
    Payment,
    PaymentStatus,
    PaymentType,
    Transaction,
)
from app.domain.repositories import (
    PaymentRepository,
    SavedCardRepository,
    TransactionRepository,
)
from app.infrastructure.azul_gateway import AzulPaymentGateway

logger = logging.getLogger(__name__)
from app.services.payment_lifecycle import serialized_3ds


class PaymentService:

    def __init__(
        self,
        payment_repo: PaymentRepository,
        txn_repo: TransactionRepository,
        gateway: AzulPaymentGateway,
        card_repo: SavedCardRepository | None = None,
        db_session: AsyncSession | None = None,
    ):
        self._payments = payment_repo
        self._txns     = txn_repo
        self._gw       = gateway
        self._cards    = card_repo
        self._db       = db_session or getattr(payment_repo, '_session', None)

    async def _send(self, payment, send, *, kind, membership=False, context=None):
        from app.services.payment_lifecycle import begin_payment, complete_payment, mark_uncertain
        if payment.amount <= 0 or payment.itbis < 0 or payment.itbis > payment.amount:
            raise ValueError('Importe o impuesto inválido.')
        payment.currency=payment.currency_code.azul_code
        payment, fresh = await begin_payment(self._db, payment, kind=kind,
            key=payment.idempotency_key, membership=membership, context=context)
        if not fresh:
            return payment, None
        try:
            payment, txn = await send(payment)
        except Exception:
            await mark_uncertain(self._db, payment.id)
            raise
        await complete_payment(self._db, payment)
        return payment, txn

    async def _persist_result(self, payment):
        from app.services.payment_lifecycle import complete_payment
        await complete_payment(self._db, payment)

    async def _save_transaction(self, txn):
        if txn is None:
            return
        try:
            await self._txns.save(txn)
        except Exception:
            await self._db.rollback()
            logger.exception('Payment result persisted; transaction audit write failed')

    async def _save_card(self, card):
        from app.services.subscription_identity import resolve_customer_identity, lock_customer_subscriptions
        identity = await resolve_customer_identity(self._db, card.customer_id)
        await lock_customer_subscriptions(self._db, identity)
        card.customer_id = identity.customer_id
        cards = []
        for identifier in identity.aliases:
            cards.extend(await self._cards.list_by_customer(identifier))
        card.is_default = not any(c.is_default for c in cards)
        return await self._cards.save_if_not_exists(card)

    async def _get_search_ids(self, customer_id: str) -> set[str]:
        from app.services.subscription_identity import resolve_customer_identity
        identity = await resolve_customer_identity(self._db, customer_id)
        return set(identity.aliases)

    # ------------------------------------------------------------------
    # One-time Sale
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_card_brand(card_number: str) -> str:
        """Detect card brand from BIN (first 1-2 digits)."""
        n = card_number.replace(" ", "").strip()
        if n.startswith("4"):
            return "VISA"
        if n.startswith(("51", "52", "53", "54", "55")):
            return "MASTERCARD"
        if n[:4] in ("2221", "2222", "2223", "2224", "2225", "2226", "2227", "2228", "2229") or n[:2] in ("23", "24", "25", "26", "27"):
            return "MASTERCARD"
        if n.startswith(("34", "37")):
            return "AMEX"
        if n.startswith(("6011", "65", "644", "645", "646", "647", "648", "649")):
            return "DISCOVER"
        return ""

    async def process_sale(
        self,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        order_id: str = "",
        auth_mode: str = "splitit",
        save_card: bool = False,
        idempotency_key: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        customer_id: str = "",
        browser_info: dict[str, str] | None = None,
        cardholder_info: dict[str, str] | None = None,
        requestor_challenge_indicator: str = "01",
        include_method_notification_url: bool = True,
        subscription_checkout: bool = False,
        activation_context: dict | None = None,
        currency: str = 'DOP',
    ) -> Payment:
        """Create and execute a one-time CIT Sale.

        cardholder_name and cardholder_email are required by Azul API v1.2.
        If save_card=True the card is stored in DataVault and the token
        is persisted on the Payment record for future use.

        When auth_mode="3dsecure", pass browser_info from the client to enable
        3DS 2.0 authentication.  The payment may end in PENDING_3DS_METHOD or
        PENDING_3DS_CHALLENGE — the caller must continue the flow via the
        /api/v1/3ds/ endpoints.
        """
        from app.domain.entities import Currency
        payment = Payment(
            currency_code=Currency(currency.upper()),
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SALE,
            order_id=order_id,
            auth_mode=auth_mode,
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            customer_id=customer_id,
        )

        if save_card:
            send = lambda p: self._gw.sale_recurring_cit(p, card_number, expiration, cvc, browser_info=browser_info)
        else:
            send = lambda p: self._gw.sale(p, card_number, expiration, cvc, save_token=False,
                browser_info=browser_info, cardholder_info=cardholder_info,
                requestor_challenge_indicator=requestor_challenge_indicator,
                include_method_notification_url=include_method_notification_url)
        context = dict(activation_context or {})
        context['card_expiration'] = expiration
        payment, txn = await self._send(payment, send, kind='sale',
            membership=subscription_checkout, context=context if subscription_checkout else {})

        # Only save the card when the payment is fully APPROVED right now.
        # If 3DS is pending, the card will be saved in the 3DS continuation
        # step to avoid duplicates.
        if (
            save_card
            and payment.data_vault_token
            and self._cards
            and customer_id
            and payment.status == PaymentStatus.APPROVED
        ):
            from app.domain.entities import SavedCard
            # Detect card brand from BIN and preserve expiration
            brand = self._detect_card_brand(card_number)
            card = SavedCard(
                customer_id=customer_id,
                token=payment.data_vault_token,
                card_brand=brand,
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                expiration=expiration,  # YYYYMM from checkout
            )
            # Auto-mark as default if this is the customer's first card
            await self._save_card(card)
            logger.warning(
                "[SVC] ✓ card saved | customer=%s brand=%s last4=%s exp=%s default=%s",
                customer_id, brand, card.card_last4, expiration, card.is_default,
            )

        logger.warning(
            "[SVC] saving payment | payment_id=%s status=%s idempotency_key=%r",
            payment.id, payment.status.value,
            payment.idempotency_key or "(none)",
        )
        try:
            await self._persist_result(payment)
        except Exception as exc:
            logger.error(
                "[SVC] ✗ payments.save FAILED | payment_id=%s type=%s msg=%s",
                payment.id, type(exc).__name__, str(exc)[:400],
            )
            raise

        try:
            await self._save_transaction(txn)
        except Exception as exc:
            logger.error(
                "[SVC] ✗ txns.save FAILED | payment_id=%s type=%s msg=%s",
                payment.id, type(exc).__name__, str(exc)[:400],
            )
            raise

        logger.warning("[SVC] payment saved OK | payment_id=%s", payment.id)
        return payment

    # ------------------------------------------------------------------
    # Service / utility payment
    # ------------------------------------------------------------------

    async def process_service_payment(
        self,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        service_type: str,
        bill_reference: str,
        order_id: str = "",
        idempotency_key: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        customer_id: str = "",
    ) -> Payment:
        """Pay a utility / service bill."""
        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SERVICE,
            order_id=order_id,
            auth_mode="splitit",
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            service_type=service_type,
            bill_reference=bill_reference,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            customer_id=customer_id,
        )

        payment, txn = await self._send(payment, lambda p: self._gw.sale(p, card_number, expiration, cvc), kind='sale')

        await self._persist_result(payment)
        await self._save_transaction(txn)
        return payment

    async def process_service_payment_with_saved_card(
        self,
        customer_id: str,
        amount: int,
        itbis: int,
        service_type: str,
        bill_reference: str,
        order_id: str = "",
        idempotency_key: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
    ) -> Payment:
        """Pay a utility / service bill using a saved DataVault token."""
        if not self._cards:
            raise ValueError("SavedCardRepository is not configured")
            
        search_ids = await self._get_search_ids(customer_id)
        cards = []
        for sid in search_ids:
            cards.extend(await self._cards.list_by_customer(sid))

        if not cards:
            raise ValueError(f"No saved cards found for customer {customer_id}")
            
        target_card = next((c for c in cards if c.is_default), cards[0])
        token = target_card.token

        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SERVICE,
            order_id=order_id,
            auth_mode="splitit",
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            service_type=service_type,
            bill_reference=bill_reference,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            customer_id=customer_id,
        )

        payment, txn = await self._send(payment, lambda p: self._gw.sale_cit(p, token), kind='sale_cit')

        await self._persist_result(payment)
        await self._save_transaction(txn)
        return payment

    async def process_hold(
        self,
        amount: int,
        itbis: int,
        card_number: str,
        expiration: str,
        cvc: str,
        order_id: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        idempotency_key: str = "",
        customer_id: str = "",
    ) -> Payment:
        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SALE,
            order_id=order_id,
            auth_mode="splitit",
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            customer_id=customer_id,
        )
        payment, txn = await self._send(payment, lambda p: self._gw.hold(p, card_number, expiration, cvc), kind='hold')
        await self._persist_result(payment)
        await self._save_transaction(txn)
        return payment

    async def process_hold_verify(
        self,
        card_number: str,
        expiration: str,
        cvc: str,
        order_id: str = "",
        idempotency_key: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        customer_id: str = "",
        browser_info: dict[str, str] | None = None,
    ) -> Payment:
        """Hold + SaveToDataVault + 3DS — verify card and tokenize without charging.

        Uses amount=100 (RD$1.00) / itbis=0 for the hold.
        The caller should void the hold after 3DS approval completes.
        The Payment.order_id starts with 'HOLD-' so post-approval handlers
        can detect it and auto-void.
        """
        payment = Payment(
            idempotency_key=idempotency_key,
            amount=100,   # RD$1.00 mínimo
            itbis=0,
            payment_type=PaymentType.SALE,
            order_id=order_id or f"HOLD-{__import__('uuid').uuid4().hex[:8].upper()}",
            auth_mode="3dsecure",
            initiated_by="cardholder",
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            customer_id=customer_id,
        )

        from app.infrastructure.azul_gateway import AzulIntegrationError
        payment, txn = await self._send(payment, lambda p: self._gw.hold_verify_card(
            p, card_number, expiration, cvc,
            browser_info=browser_info,
        ), kind='hold_verify_card')

        # Save card if immediately approved (no 3DS redirect)
        if (
            payment.data_vault_token
            and self._cards
            and customer_id
            and payment.status == PaymentStatus.APPROVED
        ):
            from app.domain.entities import SavedCard
            brand = self._detect_card_brand(card_number)
            card = SavedCard(
                customer_id=customer_id,
                token=payment.data_vault_token,
                card_brand=brand,
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                expiration=expiration,
            )
            await self._save_card(card)
            logger.warning(
                "[SVC] ✓ card saved (hold-verify) | customer=%s brand=%s last4=%s",
                customer_id, brand, card.card_last4,
            )

        await self._persist_result(payment)
        await self._save_transaction(txn)
        logger.warning("[SVC] hold-verify saved | payment_id=%s status=%s", payment.id, payment.status.value)
        return payment

    async def process_post(
        self,
        amount: int,
        itbis: int,
        azul_order_id: str,
        card_number: str = "",
        expiration: str = "",
        cvc: str = "123",
        order_id: str = "",
        cardholder_name: str = "",
        cardholder_email: str = "",
        idempotency_key: str = "",
        customer_id: str = "",
    ) -> Payment:
        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.SALE,
            order_id=order_id,
            auth_mode="splitit",
            initiated_by="merchant",
            idempotency_key=idempotency_key,
            cardholder_name=cardholder_name,
            cardholder_email=cardholder_email,
            customer_id=customer_id,
        )
        payment, txn = await self._send(payment, lambda p: self._gw.post_capture(
            p,
            azul_order_id=azul_order_id,
            card_number=card_number,
            expiration=expiration,
            cvc=cvc,
        ), kind='post_capture', context={'original_bank_order':azul_order_id})
        await self._persist_result(payment)
        await self._save_transaction(txn)
        return payment

    # ------------------------------------------------------------------
    # CIT with token — on-demand club charge
    # ------------------------------------------------------------------

    async def charge_club(
        self,
        customer_id: str,
        club_id: str,
        amount: int,
        itbis: int,
        token: str,
        idempotency_key: str = "",
    ) -> Payment:
        """Cardholder-Initiated charge for a club using a stored token.

        The user is present (e.g. tapped "Pagar" in the app) but doesn't
        re-enter card details.
        """
        payment = Payment(
            amount=amount,
            itbis=itbis,
            payment_type=PaymentType.CLUB,
            order_id=f"club-{club_id}",
            auth_mode="splitit",
            initiated_by="cardholder",
            idempotency_key=idempotency_key,
            customer_id=customer_id,
        )

        payment, txn = await self._send(payment, lambda p: self._gw.sale_cit(p, token), kind='sale_cit')

        await self._persist_result(payment)
        await self._save_transaction(txn)
        return payment

    # ------------------------------------------------------------------
    # 3DS 2.0 continuation
    # ------------------------------------------------------------------

    @serialized_3ds('method')
    async def continue_three_ds_method(
        self,
        payment_id: str,
        method_notification_status: str = "RECEIVED",
    ) -> Payment:
        """Continue 3DS after the Method iframe completed or timed out."""
        logger.warning(
            "[SVC] continue_three_ds_method | payment_id=%s method_status=%s",
            payment_id, method_notification_status,
        )

        payment = await self._payments.get_by_id(payment_id)
        if not payment:
            logger.error("[SVC] ✗ payment not found | payment_id=%s", payment_id)
            raise ValueError(f"Payment {payment_id} not found")

        logger.warning(
            "[SVC] payment found | payment_id=%s current_status=%s azul_order_id=%s",
            payment.id, payment.status.value, payment.azul_order_id,
        )

        if payment.status != PaymentStatus.PENDING_3DS_METHOD:
            logger.error(
                "[SVC] ✗ wrong status | payment_id=%s status=%s expected=PENDING_3DS_METHOD",
                payment_id, payment.status.value,
            )
            raise ValueError(
                f"Payment {payment_id} is {payment.status.value}, expected PENDING_3DS_METHOD"
            )

        data = await self._gw.process_three_ds_method(
            azul_order_id=payment.azul_order_id,
            method_notification_status=method_notification_status,
        )

        iso_raw = data.get("IsoCode", "")
        payment.iso_code = iso_raw
        payment.response_message = data.get("ResponseMessage", "")
        payment.response_code = data.get("ResponseCode", "")

        logger.warning(
            "[SVC] 3DS method result | payment_id=%s iso=%s rc=%s msg=%r",
            payment.id, iso_raw, payment.response_code, payment.response_message,
        )

        txn = Transaction(
            payment_id=payment.id,
            request_payload=f'{{"AZULOrderId":"{payment.azul_order_id}","MethodNotificationStatus":"{method_notification_status}"}}',
            response_payload=str(data),
            http_status=200,
            iso_code=iso_raw,
            response_code=payment.response_code,
            response_message=payment.response_message,
        )
        await self._save_transaction(txn)

        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
            token = data.get("DataVaultToken", "") or payment.data_vault_token or ""
            payment.data_vault_token = token
            await self._persist_result(payment)
            logger.warning("[SVC] → 3DS method approved | payment_id=%s token=%s", payment.id, token[:8] + "…" if token else "(none)")
            if token and self._cards:
                from app.domain.entities import SavedCard
                card_customer = payment.customer_id or payment.cardholder_email or payment.id
                # Try to get brand from Azul response first
                brand = data.get("DataVaultBrand", "")
                existing_card = await self._cards.get_by_token(token)
                if not brand and existing_card:
                    brand = existing_card.card_brand
                if not brand and payment.card_number_masked:
                    from app.services.post_payment import _detect_brand_from_masked
                    brand = _detect_brand_from_masked(payment.card_number_masked)

                exp = data.get("DataVaultExpiration", "")

                card = SavedCard(
                    customer_id=card_customer,
                    token=token,
                    card_brand=brand,
                    card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                    expiration=exp,
                )
                # Auto-mark as default if first card
                try:
                    await self._save_card(card)
                    logger.warning("[SVC] ✓ token persisted (method step) | payment_id=%s customer=%s brand=%s", payment.id, card_customer, brand)
                except Exception as exc:
                    await self._db.rollback()
                    logger.error("[SVC] ✗ card save FAILED (method step) | payment_id=%s err=%s", payment.id, exc)
        elif iso_raw == IsoCode.THREE_DS_CHALLENGE:
            payment.status = PaymentStatus.PENDING_3DS_CHALLENGE
            challenge_data = data.get("ThreeDSChallenge", {})

            if isinstance(challenge_data, dict):
                # Azul returns RedirectPostUrl + CReq (EMV 3DS 2.x Cardinal Commerce format)
                redirect_post_url = (
                    challenge_data.get("RedirectPostUrl")
                    or challenge_data.get("RedirectUrl")
                    or data.get("RedirectUrl", "")
                )
                creq = challenge_data.get("CReq", "")

                logger.warning(
                    "[SVC] 3DS challenge fields | redirect_post_url=%r creq_len=%d "
                    "challenge_keys=%s",
                    redirect_post_url[:80] if redirect_post_url else "",
                    len(creq),
                    list(challenge_data.keys()),
                )

                if redirect_post_url and not redirect_post_url.startswith("http"):
                    logger.error("[SVC] ✗ Invalid 3DS challenge URL returned by Azul: %r", redirect_post_url)
                    payment.status = PaymentStatus.DECLINED
                    payment.response_message = "Error en comunicación con el banco (URL inválida)"
                elif redirect_post_url and creq:
                    # Build auto-submit POST form — browser POSTs directly to ACS (Cardinal/bank).
                    # Form submissions are NOT subject to CORS restrictions, so we submit
                    # immediately on DOMContentLoaded — the industry-standard 3DS approach.
                    payment.threeds_redirect_url = redirect_post_url
                    payment.threeds_challenge_form = (
                        f'<!DOCTYPE html>'
                        f'<html lang="es">'
                        f'<head>'
                        f'<meta charset="UTF-8"/>'
                        f'<meta name="viewport" content="width=device-width,initial-scale=1"/>'
                        f'<title>Autenticando con tu banco…</title>'
                        f'<meta http-equiv="Content-Security-Policy" '
                        f'content="script-src \'self\' \'unsafe-inline\'; '
                        f'form-action https://authentication.cardinalcommerce.com https://*.cardinalcommerce.com;"/>'
                        f'<style>'
                        f'body{{margin:0;background:#0f0f1a;color:#e2e8f0;'
                        f'font-family:system-ui,sans-serif;display:flex;align-items:center;'
                        f'justify-content:center;min-height:100vh;text-align:center;}}'
                        f'.box{{padding:2rem;max-width:360px;}}'
                        f'.ring{{width:48px;height:48px;border:4px solid rgba(108,99,255,.3);'
                        f'border-top-color:#6c63ff;border-radius:50%;'
                        f'animation:spin .9s linear infinite;margin:0 auto 1.2rem;}}'
                        f'@keyframes spin{{to{{transform:rotate(360deg)}}}}'
                        f'h2{{font-size:1.1rem;font-weight:600;margin-bottom:.5rem;}}'
                        f'p{{color:#94a3b8;font-size:.88rem;margin-bottom:1.5rem;}}'
                        f'.btn{{padding:.8rem 1.5rem;font-size:1rem;cursor:pointer;background:#6c63ff;color:#fff;border:none;border-radius:.6rem;display:none;margin:0 auto;}}'
                        f'</style>'
                        f'</head>'
                        f'<body>'
                        f'<div class="box">'
                        f'  <div class="ring" id="loader"></div>'
                        f'  <h2 id="title">Autenticando con tu banco…</h2>'
                        f'  <p id="desc">Serás redirigido en un momento.<br/>No cierres esta ventana.</p>'
                        f'  <form id="cform" method="POST" action="{redirect_post_url}">'
                        f'    <input type="hidden" name="creq" value="{creq}"/>'
                        f'    <button type="submit" id="btnContinue" class="btn">Continuar con tu banco &rarr;</button>'
                        f'  </form>'
                        f'</div>'
                        f'<script>'
                        f'(function(){{'
                        f'  function doSubmit(){{'
                        f'    var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);'
                        f'    var isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);'
                        f'    var form = document.getElementById("cform");'
                        f'    if (isIOS || isSafari) {{'
                        f'      document.getElementById("loader").style.display = "none";'
                        f'      document.getElementById("title").innerText = "Validación requerida";'
                        f'      document.getElementById("desc").innerHTML = "Tu banco requiere validación adicional.<br/>Presiona el botón para continuar de forma segura.";'
                        f'      document.getElementById("btnContinue").style.display = "inline-block";'
                        f'    }} else {{'
                        f'      try{{form.submit();}}'
                        f'      catch(e){{console.error("3DS submit error",e);}}'
                        f'    }}'
                        f'  }}'
                        f'  if(document.readyState==="loading"){{'
                        f'    document.addEventListener("DOMContentLoaded",doSubmit);'
                        f'  }}else{{'
                        f'    doSubmit();'
                        f'  }}'
                        f'}})();'
                        f'</script>'
                        f'</body></html>'
                    )
                    logger.warning(
                        "[SVC] built challenge form | payment_id=%s url=%s creq_len=%d",
                        payment.id, redirect_post_url[:60], len(creq),
                    )
                elif redirect_post_url:
                    payment.threeds_redirect_url = redirect_post_url
                else:
                    # Fallback: try legacy ChallengeForm field
                    payment.threeds_challenge_form = challenge_data.get("ChallengeForm", "")
                    payment.threeds_redirect_url = challenge_data.get("RedirectUrl", "") or data.get("RedirectUrl", "")

            logger.warning(
                "[SVC] → 3DS challenge needed | payment_id=%s form_len=%d redirect=%r",
                payment.id,
                len(payment.threeds_challenge_form or ""),
                (payment.threeds_redirect_url or "")[:80],
            )

        else:
            payment.status = PaymentStatus.DECLINED
            logger.warning(
                "[SVC] → 3DS declined | payment_id=%s iso=%s msg=%r",
                payment.id, iso_raw, payment.response_message,
            )

        payment.threeds_method_form = ""
        await self._persist_result(payment)
        logger.warning("[SVC] payment updated | payment_id=%s final_status=%s", payment.id, payment.status.value)
        return payment

    @serialized_3ds('challenge')
    async def continue_three_ds_challenge(
        self,
        payment_id: str,
        cres: str = "",
    ) -> Payment:
        """Complete 3DS after the cardholder finished the ACS challenge.

        Called by the TermUrl callback when the bank redirects back.
        """
        payment = await self._payments.get_by_id(payment_id)
        if not payment:
            raise ValueError(f"Payment {payment_id} not found")
        if payment.status != PaymentStatus.PENDING_3DS_CHALLENGE:
            raise ValueError(
                f"Payment {payment_id} is {payment.status.value}, expected PENDING_3DS_CHALLENGE"
            )

        data = await self._gw.process_three_ds_challenge(
            azul_order_id=payment.azul_order_id,
            cres=cres,
        )

        iso_raw = data.get("IsoCode", "")
        payment.iso_code = iso_raw
        payment.response_message = data.get("ResponseMessage", "")
        payment.response_code = data.get("ResponseCode", "")

        masked_cres = "***" if cres else ""
        txn = Transaction(
            payment_id=payment.id,
            request_payload=f'{{"AZULOrderId":"{payment.azul_order_id}","cRes":"{masked_cres}"}}',
            response_payload=str(data),
            http_status=200,
            iso_code=iso_raw,
            response_code=payment.response_code,
            response_message=payment.response_message,
        )
        await self._save_transaction(txn)

        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
            token = data.get("DataVaultToken", "") or payment.data_vault_token or ""
            payment.data_vault_token = token
            await self._persist_result(payment)
            # Persistir token en SavedCardRepository para cobros mensuales futuros
            if token and self._cards:
                from app.domain.entities import SavedCard
                card_customer = payment.customer_id or payment.cardholder_email or payment.id
                # Try to get brand from Azul response first
                brand = data.get("DataVaultBrand", "")
                existing_card = await self._cards.get_by_token(token)
                if not brand and existing_card:
                    brand = existing_card.card_brand
                if not brand and payment.card_number_masked:
                    from app.services.post_payment import _detect_brand_from_masked
                    brand = _detect_brand_from_masked(payment.card_number_masked)
                
                exp = data.get("DataVaultExpiration", "")

                card = SavedCard(
                    customer_id=card_customer,
                    token=token,
                    card_brand=brand,
                    card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                    expiration=exp,
                )
                # Auto-mark as default if first card
                try:
                    await self._save_card(card)
                    logger.warning(
                        "[SVC] ✓ DataVaultToken persisted | payment_id=%s customer=%s token=%s brand=%s",
                        payment.id, card_customer, token[:8] + "…", brand,
                    )
                except Exception as exc:
                    await self._db.rollback()
                    logger.error(
                        "[SVC] ✗ card save FAILED | payment_id=%s err=%s",
                        payment.id, exc,
                    )
        else:
            payment.status = PaymentStatus.DECLINED

        payment.threeds_redirect_url = ""
        payment.threeds_challenge_form = ""
        await self._persist_result(payment)
        return payment

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._payments.get_by_id(payment_id)

    async def verify_payment(self, custom_order_id: str) -> dict:
        return await self._gw.verify_payment(custom_order_id=custom_order_id)
