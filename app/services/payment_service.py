"""
Payment service — orchestrates single payments and service payments.

Idempotency
-----------
If an ``idempotency_key`` is provided and a payment with that key already
exists in the database, the service returns the existing record WITHOUT
calling Azul again.  This prevents double charges on client retries.
"""

from __future__ import annotations

import logging

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


class PaymentService:

    def __init__(
        self,
        payment_repo: PaymentRepository,
        txn_repo: TransactionRepository,
        gateway: AzulPaymentGateway,
        card_repo: SavedCardRepository | None = None,
    ):
        self._payments = payment_repo
        self._txns     = txn_repo
        self._gw       = gateway
        self._cards    = card_repo

    # ------------------------------------------------------------------
    # One-time Sale
    # ------------------------------------------------------------------

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
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        payment = Payment(
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
            # Suscripción: usa STANDING_ORDER indicator + SaveToDataVault=1
            # Fallback: si Azul no tiene DataVault habilitado aún, reintenta sin token
            from app.infrastructure.azul_gateway import AzulIntegrationError
            try:
                payment, txn = await self._gw.sale_recurring_cit(
                    payment, card_number, expiration, cvc,
                    browser_info=browser_info,
                )
            except AzulIntegrationError as exc:
                if "datavault not enabled" in str(exc).lower():
                    logger.warning(
                        "[SVC] ⚠ DataVault not enabled — falling back to sale() without token | payment_id=%s",
                        payment.id,
                    )
                    payment, txn = await self._gw.sale(
                        payment, card_number, expiration, cvc,
                        save_token=False,
                        browser_info=browser_info,
                        cardholder_info=cardholder_info,
                        requestor_challenge_indicator=requestor_challenge_indicator,
                        include_method_notification_url=include_method_notification_url,
                    )
                else:
                    raise
        else:
            payment, txn = await self._gw.sale(
                payment, card_number, expiration, cvc,
                save_token=False,
                browser_info=browser_info,
                cardholder_info=cardholder_info,
                requestor_challenge_indicator=requestor_challenge_indicator,
                include_method_notification_url=include_method_notification_url,
            )

        if save_card and payment.data_vault_token and self._cards:
            from app.domain.entities import SavedCard
            card = SavedCard(
                customer_id=customer_id or order_id or payment.id,
                token=payment.data_vault_token,
                card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
            )
            await self._cards.save(card)

        logger.warning(
            "[SVC] saving payment | payment_id=%s status=%s idempotency_key=%r",
            payment.id, payment.status.value,
            payment.idempotency_key or "(none)",
        )
        try:
            await self._payments.save(payment)
        except Exception as exc:
            logger.error(
                "[SVC] ✗ payments.save FAILED | payment_id=%s type=%s msg=%s",
                payment.id, type(exc).__name__, str(exc)[:400],
            )
            raise

        try:
            await self._txns.save(txn)
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
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

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

        payment, txn = await self._gw.sale(payment, card_number, expiration, cvc)

        await self._payments.save(payment)
        await self._txns.save(txn)
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
            
        cards = await self._cards.list_by_customer(customer_id)
        if not cards:
            raise ValueError(f"No saved cards found for customer {customer_id}")
            
        target_card = next((c for c in cards if c.is_default), cards[0])
        token = target_card.token

        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

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

        payment, txn = await self._gw.sale_cit(payment, token)

        await self._payments.save(payment)
        await self._txns.save(txn)
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
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

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
        payment, txn = await self._gw.hold(payment, card_number, expiration, cvc)
        await self._payments.save(payment)
        await self._txns.save(txn)
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
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

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
        payment, txn = await self._gw.post_capture(
            payment,
            azul_order_id=azul_order_id,
            card_number=card_number,
            expiration=expiration,
            cvc=cvc,
        )
        await self._payments.save(payment)
        await self._txns.save(txn)
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
        if idempotency_key:
            existing = await self._payments.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing

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

        payment, txn = await self._gw.sale_cit(payment, token)

        await self._payments.save(payment)
        await self._txns.save(txn)
        return payment

    # ------------------------------------------------------------------
    # 3DS 2.0 continuation
    # ------------------------------------------------------------------

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
        await self._txns.save(txn)

        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
            token = data.get("DataVaultToken", "") or payment.data_vault_token or ""
            payment.data_vault_token = token
            logger.warning("[SVC] → 3DS method approved | payment_id=%s token=%s", payment.id, token[:8] + "…" if token else "(none)")
            if token and self._cards:
                from app.domain.entities import SavedCard
                card = SavedCard(
                    customer_id=payment.cardholder_email or payment.order_id or payment.id,
                    token=token,
                    card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                )
                try:
                    await self._cards.save(card)
                    logger.warning("[SVC] ✓ token persisted (method step) | payment_id=%s", payment.id)
                except Exception as exc:
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

                if redirect_post_url and creq:
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
                        f'<title>Autenticando con tu banco\u2026</title>'
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
                        f'</style>'
                        f'</head>'
                        f'<body>'
                        f'<div class="box">'
                        f'  <div class="ring"></div>'
                        f'  <h2>Autenticando con tu banco\u2026</h2>'
                        f'  <p>Ser\u00e1s redirigido en un momento.<br/>No cierres esta ventana.</p>'
                        f'</div>'
                        f'<form id="cform" method="POST" action="{redirect_post_url}" style="display:none;">'
                        f'  <input type="hidden" name="creq" value="{creq}"/>'
                        f'</form>'
                        f'<noscript>'
                        f'  <div style="margin-top:1.5rem">'
                        f'    <form method="POST" action="{redirect_post_url}">'
                        f'      <input type="hidden" name="creq" value="{creq}"/>'
                        f'      <button type="submit" style="padding:.8rem 1.5rem;font-size:1rem;'
                        f'cursor:pointer;background:#6c63ff;color:#fff;border:none;border-radius:.6rem;">'
                        f'Continuar con tu banco &rarr;</button>'
                        f'    </form>'
                        f'  </div>'
                        f'</noscript>'
                        f'<script>'
                        f'(function(){{'
                        f'  function doSubmit(){{'
                        f'    try{{document.getElementById("cform").submit();}}'
                        f'    catch(e){{console.error("3DS submit error",e);}}'
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
        await self._payments.update(payment)
        logger.warning("[SVC] payment updated | payment_id=%s final_status=%s", payment.id, payment.status.value)
        return payment

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
        await self._txns.save(txn)

        if iso_raw == IsoCode.APPROVED:
            payment.status = PaymentStatus.APPROVED
            token = data.get("DataVaultToken", "") or payment.data_vault_token or ""
            payment.data_vault_token = token
            # Persistir token en SavedCardRepository para cobros mensuales futuros
            if token and self._cards:
                from app.domain.entities import SavedCard
                card = SavedCard(
                    customer_id=payment.cardholder_email or payment.order_id or payment.id,
                    token=token,
                    card_last4=payment.card_number_masked[-4:] if payment.card_number_masked else "",
                )
                try:
                    await self._cards.save(card)
                    logger.warning(
                        "[SVC] ✓ DataVaultToken persisted | payment_id=%s customer=%s token=%s",
                        payment.id,
                        card.customer_id,
                        token[:8] + "…",
                    )
                except Exception as exc:
                    logger.error(
                        "[SVC] ✗ card save FAILED | payment_id=%s err=%s",
                        payment.id, exc,
                    )
        else:
            payment.status = PaymentStatus.DECLINED

        payment.threeds_redirect_url = ""
        payment.threeds_challenge_form = ""
        await self._payments.update(payment)
        return payment

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def get_payment(self, payment_id: str) -> Payment | None:
        return await self._payments.get_by_id(payment_id)

    async def verify_payment(self, custom_order_id: str) -> dict:
        return await self._gw.verify_payment(custom_order_id=custom_order_id)
