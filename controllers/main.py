# -*- coding: utf-8 -*-
"""
eBay Webhook Controller
=======================
Handles two request types sent by the eBay Notification API:

GET  /ebay/webhook/event?challenge_code=XXX
    eBay endpoint-verification challenge.  Responds with the SHA-256 hash of
    (challengeCode + verificationToken + endpointUrl) as per eBay docs.

POST /ebay/webhook/event
    Incoming event notification (order paid, cancelled, etc.).
    Delegates order processing to the sale.order model.
"""

import hashlib
import json
import logging

from odoo import http
from odoo.http import Response, request

_logger = logging.getLogger(__name__)

# eBay notification topics that require an order import attempt
_ORDER_TOPICS = frozenset([
    'marketplace.order.payment_completion',
    'marketplace.order.created',
    'MARKETPLACE_ORDER_PAYMENT_COMPLETION',
    'MARKETPLACE_ORDER_CREATED',
])


class EbayWebhookController(http.Controller):

    @http.route(
        '/ebay/webhook/event',
        type='http',
        auth='public',
        csrf=False,
        methods=['GET', 'POST'],
        save_session=False,
    )
    def ebay_webhook_event(self, **kwargs):
        """Single endpoint for both eBay challenge verification and event delivery."""
        if request.httprequest.method == 'GET':
            return self._handle_challenge(kwargs)
        return self._handle_notification()

    # ------------------------------------------------------------------
    # Challenge-response verification (eBay GET)
    # ------------------------------------------------------------------

    def _handle_challenge(self, params):
        """
        Compute and return the SHA-256 challenge response required by eBay
        before it will start delivering notifications to this endpoint.

        Algorithm (per eBay documentation):
            hash = SHA256(challengeCode + verificationToken + endpointUrl)
        """
        challenge_code = params.get('challenge_code', '').strip()
        if not challenge_code:
            _logger.warning("eBay webhook GET received without challenge_code")
            return Response('Missing challenge_code parameter', status=400)

        # Find the verification token from the first active instance that has one
        instance = (
            request.env['ebay.instance']
            .sudo()
            .search(
                [
                    ('webhook_verification_token', '!=', False),
                    ('webhook_verification_token', '!=', ''),
                    ('active', '=', True),
                ],
                limit=1,
            )
        )
        if not instance:
            _logger.error(
                "eBay webhook challenge failed: no instance has a "
                "webhook_verification_token configured."
            )
            return Response('Verification token not configured', status=500)

        # The endpoint URL must exactly match the URL registered in the
        # eBay Developer Portal (scheme + host + path, no query string).
        endpoint_url = request.httprequest.url.split('?')[0]

        raw = challenge_code + instance.webhook_verification_token + endpoint_url
        challenge_response = hashlib.sha256(raw.encode('utf-8')).hexdigest()

        _logger.info(
            "eBay webhook challenge verified for instance '%s'", instance.name
        )
        return Response(
            json.dumps({'challengeResponse': challenge_response}),
            content_type='application/json',
            status=200,
        )

    # ------------------------------------------------------------------
    # Incoming event notification (eBay POST)
    # ------------------------------------------------------------------

    def _handle_notification(self):
        """
        Parse an incoming eBay event notification and dispatch to the
        appropriate handler.  Always returns HTTP 200 to eBay regardless
        of internal errors (eBay will retry on non-200 responses).
        """
        raw_body = request.httprequest.data
        if not raw_body:
            _logger.warning("eBay webhook POST received with empty body")
            return Response(status=200)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            _logger.warning("eBay webhook: JSON decode error — %s", exc)
            return Response(status=200)

        topic = payload.get('topic') or payload.get('notificationId', '')
        _logger.info("eBay webhook notification received: topic=%s", topic)

        # Dispatch order-related events
        if topic in _ORDER_TOPICS or any(t in topic for t in ('order', 'payment')):
            self._dispatch_order_event(payload)

        return Response(status=200)

    def _dispatch_order_event(self, payload):
        """
        Extract the eBay orderId from the notification payload, fetch the
        full order from eBay, then call sale.order.process_ebay_order().
        """
        # eBay wraps the data in different keys depending on the notification schema
        data = payload.get('data') or payload.get('notification', {}).get('data', {})
        ebay_order_id = data.get('orderId') or data.get('orderId')

        if not ebay_order_id:
            _logger.warning(
                "eBay webhook order event has no orderId — payload keys: %s",
                list(payload.keys()),
            )
            return

        # Identify the instance by sellerId when available; otherwise use the
        # first active connected instance (common in single-account deployments)
        seller_id = data.get('sellerId') or payload.get('notification', {}).get('sellerId')

        domain = [('active', '=', True), ('connection_status', '=', 'connected')]
        # Note: future enhancement — store sellerId on ebay.instance and filter here
        instance = request.env['ebay.instance'].sudo().search(domain, limit=1)

        if not instance:
            _logger.error(
                "eBay webhook: no connected instance found to process order %s",
                ebay_order_id,
            )
            return

        try:
            # Fetch the full order details from eBay and create/update the SO
            instance.sudo()._fetch_single_order(ebay_order_id)
        except Exception:
            _logger.exception(
                "eBay webhook: unhandled error processing order %s on instance '%s'",
                ebay_order_id,
                instance.name,
            )
