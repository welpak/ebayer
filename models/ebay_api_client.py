# -*- coding: utf-8 -*-
"""
eBay API Client
===============
Two layers:

1. ``EbayApiClient``  — a plain Python helper instantiated per request.
   Wraps OAuth token management and all raw HTTP calls to the eBay REST APIs.
   Not an Odoo model; never touches self.env.

2. ``EbayInstanceApiMethods`` — an ORM extension of ``ebay.instance`` that:
   * adds operational fields (warehouse, company, verification token, …)
   * exposes Odoo-callable methods (_cron_*, action_test_connection, …)
   * delegates every actual HTTP call to EbayApiClient
"""

import base64
import json
import logging
from datetime import datetime, timedelta

import requests
from requests.exceptions import RequestException

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Carrier name → eBay shipping carrier code mapping
# ---------------------------------------------------------------------------
_CARRIER_CODE_MAP = {
    'ups': 'UPS',
    'fedex': 'FEDEX',
    'usps': 'USPS',
    'united states postal service': 'USPS',
    'dhl': 'DHL_EXPRESS_1200',
    'dhl express': 'DHL_EXPRESS_1200',
    'royal mail': 'ROYALMAIL',
    'parcelforce': 'PARCELFORCE',
    'tnt': 'TNT',
    'ontrac': 'ONTRAC',
    'lasership': 'LASERSHIP',
    'canada post': 'CANADA_POST',
    'australia post': 'AUSTRALIA_POST',
    'purolator': 'PUROLATOR',
    'gls': 'GLS',
    'hermes': 'HERMES',
    'dpd': 'DPD',
    'evri': 'HERMES',
    'colissimo': 'COLISSIMO',
    'chronopost': 'CHRONOPOST',
    'correos': 'CORREOS',
    'poste italiane': 'POSTE_ITALIANE',
    'bpost': 'BPOST',
    'swiss post': 'SWISS_POST',
    'japan post': 'JAPAN_POST',
    'sf express': 'SF_EXPRESS',
}


def _carrier_to_ebay_code(carrier_name):
    """Return the eBay carrier code for a carrier name, falling back to 'OTHER'."""
    if not carrier_name:
        return 'OTHER'
    return _CARRIER_CODE_MAP.get(carrier_name.lower().strip(), 'OTHER')


# ---------------------------------------------------------------------------
# EbayApiClient  (pure Python, not an Odoo model)
# ---------------------------------------------------------------------------

class EbayApiClient:
    """
    Stateless wrapper around the eBay REST API for a single ebay.instance record.

    Usage::

        client = EbayApiClient(instance)
        data   = client.get('/sell/fulfillment/v1/order', filter='...')
        client.post('/sell/inventory/v1/bulk_update_price_quantity', payload)
    """

    _PROD_BASE    = 'https://api.ebay.com'
    _SANDBOX_BASE = 'https://api.sandbox.ebay.com'

    # eBay Sell API scopes required for all connector operations
    _USER_SCOPES = (
        'https://api.ebay.com/oauth/api_scope/sell.fulfillment '
        'https://api.ebay.com/oauth/api_scope/sell.inventory '
        'https://api.ebay.com/oauth/api_scope/sell.account'
    )

    def __init__(self, instance):
        self.instance = instance
        self.base_url = (
            self._PROD_BASE
            if instance.environment == 'production'
            else self._SANDBOX_BASE
        )
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

    # ------------------------------------------------------------------
    # OAuth helpers
    # ------------------------------------------------------------------

    def _basic_auth_header(self):
        """Return a Basic Auth header value for this instance's app credentials."""
        raw = f"{self.instance.app_id}:{self.instance.cert_id}"
        encoded = base64.b64encode(raw.encode('utf-8')).decode('utf-8')
        return f"Basic {encoded}"

    def _token_endpoint(self):
        return f"{self.base_url}/identity/v1/oauth2/token"

    def get_app_token(self):
        """
        Obtain an application-level OAuth token via the Client Credentials grant.
        Used for APIs that don't require user authorization (e.g., Browse API).
        Returns the access_token string.
        """
        resp = self._session.post(
            self._token_endpoint(),
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': self._basic_auth_header(),
            },
            data={
                'grant_type': 'client_credentials',
                'scope': 'https://api.ebay.com/oauth/api_scope',
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()['access_token']

    def refresh_user_token(self):
        """
        Exchange the stored refresh_token for a new access_token.
        Persists the new token + expiry to the ebay.instance record.
        Returns the new access_token string.
        Raises UserError when no refresh token is available.
        """
        if not self.instance.refresh_token:
            raise UserError(
                _("No refresh token is stored for eBay instance '%s'. "
                  "Please complete the OAuth authorisation flow first.")
                % self.instance.name
            )

        resp = self._session.post(
            self._token_endpoint(),
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': self._basic_auth_header(),
            },
            data={
                'grant_type': 'refresh_token',
                'refresh_token': self.instance.refresh_token,
                'scope': self._USER_SCOPES,
            },
            timeout=30,
        )
        try:
            resp.raise_for_status()
        except RequestException as exc:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise UserError(
                _("eBay token refresh failed for instance '%s': %s\n%s")
                % (self.instance.name, exc, detail)
            ) from exc

        payload     = resp.json()
        new_token   = payload['access_token']
        expires_in  = int(payload.get('expires_in', 7200))
        # Subtract 60 s as a safety margin so we never use an about-to-expire token
        expires_at  = datetime.utcnow() + timedelta(seconds=expires_in - 60)

        self.instance.sudo().write({
            'access_token':     new_token,
            'token_expires_at': expires_at,
            'connection_status': 'connected',
        })
        _logger.info(
            "eBay: access token refreshed for instance '%s', expires %s",
            self.instance.name, expires_at.isoformat(),
        )
        return new_token

    def get_valid_token(self):
        """
        Return a valid user access token, transparently refreshing when expired.
        """
        if not self.instance.access_token or self.instance.is_token_expired:
            return self.refresh_user_token()
        return self.instance.access_token

    # ------------------------------------------------------------------
    # Core HTTP method
    # ------------------------------------------------------------------

    def make_request(self, method, path, use_user_token=True, **kwargs):
        """
        Execute an authenticated eBay REST request.

        :param method:           HTTP verb ('GET', 'POST', 'PUT', 'DELETE').
        :param path:             API path, e.g. '/sell/fulfillment/v1/order'.
        :param use_user_token:   True → use user OAuth token (default).
                                 False → use application-level token.
        :param kwargs:           Forwarded to requests.Session.request().
        :returns:                Parsed JSON dict (empty dict for 204/no-body).
        :raises requests.HTTPError: on non-2xx responses (with body logged).
        """
        url = f"{self.base_url}{path}"

        token = (
            self.get_valid_token() if use_user_token else self.get_app_token()
        )

        extra_headers = kwargs.pop('headers', {})
        headers = {
            'Authorization': f"Bearer {token}",
            'Content-Type':  'application/json',
            'Accept':        'application/json',
            'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        }
        headers.update(extra_headers)

        resp = self._session.request(
            method, url, headers=headers, timeout=30, **kwargs
        )

        if not resp.ok:
            body = {}
            try:
                body = resp.json()
            except Exception:
                body = {'raw': resp.text[:500]}

            _logger.error(
                "eBay API error %s %s → HTTP %s: %s",
                method, path, resp.status_code, json.dumps(body)[:1000],
            )

            # Build a human-readable message from eBay's structured error response
            ebay_msgs = []
            for err in body.get('errors', [])[:5]:
                msg  = err.get('longMessage') or err.get('message') or ''
                params = err.get('parameters', [])
                if params:
                    param_str = ', '.join(
                        f"{p.get('name')}={p.get('value')}" for p in params
                    )
                    msg = f"{msg} [{param_str}]"
                if msg:
                    ebay_msgs.append(f"errorId {err.get('errorId','?')}: {msg}")

            if not ebay_msgs:
                raw = body.get('message') or body.get('raw') or ''
                if raw:
                    ebay_msgs.append(str(raw)[:300])

            detail = '\n'.join(ebay_msgs) if ebay_msgs else f"HTTP {resp.status_code}"

            from requests.exceptions import HTTPError
            raise HTTPError(
                f"HTTP {resp.status_code} {method} {path}:\n{detail}",
                response=resp,
            )

        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get(self, path, **params):
        """HTTP GET with optional query parameters."""
        return self.make_request('GET', path, params=params or None)

    def post(self, path, payload, headers=None):
        """HTTP POST with a JSON body."""
        kwargs = {'json': payload}
        if headers:
            kwargs['headers'] = headers
        return self.make_request('POST', path, **kwargs)

    def put(self, path, payload, headers=None):
        """HTTP PUT with a JSON body."""
        kwargs = {'json': payload}
        if headers:
            kwargs['headers'] = headers
        return self.make_request('PUT', path, **kwargs)

    def delete(self, path):
        """HTTP DELETE."""
        return self.make_request('DELETE', path)


# ---------------------------------------------------------------------------
# ebay.instance extension — operational fields + high-level API methods
# ---------------------------------------------------------------------------

class EbayInstanceApiMethods(models.Model):
    """
    Extends ebay.instance with:
      - warehouse, company, webhook token, last-sync timestamp
      - connection test action
      - cron entry points (_cron_fetch_orders, _cron_sync_inventory)
      - order-fetching helpers used by both the cron and the webhook controller
      - inventory push helpers used by product.py
    """

    _inherit = 'ebay.instance'

    # ------------------------------------------------------------------
    # Additional fields
    # ------------------------------------------------------------------

    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        help='Odoo company to use when creating orders and stock records.',
    )
    warehouse_id = fields.Many2one(
        comodel_name='stock.warehouse',
        string='Warehouse',
        default=lambda self: self.env['stock.warehouse'].search([], limit=1),
        help='Warehouse used when confirming eBay sale orders.',
    )
    webhook_verification_token = fields.Char(
        string='Webhook Verification Token',
        help='Token configured in the eBay Developer Portal under '
             'Notifications → Endpoint. Used to verify ownership of this '
             'webhook endpoint via the SHA-256 challenge-response protocol.',
    )
    last_order_sync = fields.Datetime(
        string='Last Order Sync',
        readonly=True,
        copy=False,
        help='Timestamp of the most recent successful order fetch from eBay.',
    )

    # ------------------------------------------------------------------
    # Listing-sync fields (Phase 4)
    # ------------------------------------------------------------------
    enable_listing_sync = fields.Boolean(
        string='Enable Listing Sync (Pull from eBay)',
        default=False,
        help='When enabled, the batch cron will pull eBay listings into Odoo '
             'and the webhook controller will process listing-change events.',
    )
    last_listing_pull_sync = fields.Datetime(
        string='Last Listing Pull Sync',
        readonly=True,
        copy=False,
        help='UTC timestamp of the most recent successful listing pull from eBay.',
    )
    default_marketplace_id = fields.Char(
        string='Default Marketplace',
        default='EBAY_US',
        help='Default eBay marketplace used when publishing listings from this '
             'instance (e.g. EBAY_US, EBAY_GB, EBAY_DE). '
             'Can be overridden per product mapping.',
    )
    default_fulfillment_policy_id = fields.Char(
        string='Default Fulfillment Policy ID',
        help='eBay fulfillment policy ID applied to listings that do not '
             'specify their own. Find policy IDs in Seller Hub → Policies.',
    )
    default_payment_policy_id = fields.Char(
        string='Default Payment Policy ID',
        help='eBay payment policy ID applied to listings that do not specify '
             'their own.',
    )
    default_return_policy_id = fields.Char(
        string='Default Return Policy ID',
        help='eBay return policy ID applied to listings that do not specify '
             'their own.',
    )

    # ------------------------------------------------------------------
    # Order-export fields (Phase 4)
    # ------------------------------------------------------------------
    enable_order_export = fields.Boolean(
        string='Enable Order Export to eBay',
        default=False,
        help='When enabled, confirmed Odoo sale orders marked for export will '
             'be pushed to eBay as Buy-It-Now listings.',
    )

    # ------------------------------------------------------------------
    # Merchant location (required for Item.Country when publishing)
    # ------------------------------------------------------------------
    country_code = fields.Char(
        string='Seller Country Code',
        size=2,
        default='US',
        help='Two-letter ISO 3166-1 alpha-2 country code of this seller\'s '
             'location (e.g. US, GB, DE, AU). Used to create/maintain the '
             'eBay merchant location record that eBay needs to resolve '
             'Item.Country when a listing is published.',
    )

    # ------------------------------------------------------------------
    # API client factory
    # ------------------------------------------------------------------

    def _get_api_client(self):
        """Return an EbayApiClient bound to this instance."""
        self.ensure_one()
        return EbayApiClient(self)

    # ------------------------------------------------------------------
    # Merchant location helper
    # ------------------------------------------------------------------

    def _get_or_create_merchant_location(self, client):
        """
        Return a merchant location key for this instance, upserting a minimal
        default location if needed.

        eBay's Inventory API requires at least one merchant location so it can
        populate Item.Country when a listing is published.  Without it the
        publish call fails with errorId 25002.

        Uses 'odoo-default' as the location key (idempotent PUT).
        Falls back to the first enabled existing location if the upsert fails.
        Returns None if no location can be obtained (publish will be attempted
        without a distribution set and may fail depending on account config).
        """
        self.ensure_one()
        location_key = 'odoo-default'
        country = (self.country_code or 'US').upper()[:2]

        try:
            client.put(
                f'/sell/inventory/v1/location/{location_key}',
                {
                    'location': {
                        'address': {
                            'country': country,
                        },
                    },
                    'merchantLocationStatus': 'ENABLED',
                    'name': 'Odoo Default Location',
                },
            )
            return location_key
        except Exception as exc:
            _logger.warning(
                "eBay: could not upsert merchant location '%s' for instance '%s': %s",
                location_key, self.name, exc,
            )

        # Fallback: list existing locations and use the first enabled one
        try:
            resp = client.get('/sell/inventory/v1/location')
            for loc in resp.get('locations', []):
                if loc.get('merchantLocationStatus') == 'ENABLED':
                    return loc['merchantLocationKey']
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def action_test_connection(self):
        """Attempt to refresh the user token and mark the instance connected."""
        self.ensure_one()
        try:
            client = EbayApiClient(self)
            client.refresh_user_token()
        except Exception as exc:
            self.sudo().write({'connection_status': 'error'})
            raise UserError(
                _("eBay connection test failed for '%s':\n%s") % (self.name, exc)
            ) from exc

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _("Connection Successful"),
                'message': _("Authenticated with eBay for instance '%s'.") % self.name,
                'type':    'success',
                'sticky':  False,
            },
        }

    # ------------------------------------------------------------------
    # Cron entry points
    # ------------------------------------------------------------------

    def _cron_fetch_orders(self):
        """
        Called by ir.cron.  Iterates all active, connected instances and
        imports any new eBay orders.  Never raises — errors are isolated
        per instance so one bad account cannot block others.
        """
        instances = self.search([
            ('active', '=', True),
            ('connection_status', '=', 'connected'),
            ('enable_batch_sync', '=', True),
        ])
        for instance in instances:
            try:
                instance._fetch_and_process_orders()
            except Exception:
                _logger.exception(
                    "eBay _cron_fetch_orders: unhandled error on instance "
                    "'%s' (id=%s)", instance.name, instance.id,
                )

    def _cron_sync_inventory(self):
        """
        Called by ir.cron.  Pushes current Odoo stock levels to eBay for
        all active inventory-sync mappings.
        """
        instances = self.search([
            ('active', '=', True),
            ('connection_status', '=', 'connected'),
            ('enable_batch_sync', '=', True),
        ])
        for instance in instances:
            try:
                instance._push_all_inventory()
            except Exception:
                _logger.exception(
                    "eBay _cron_sync_inventory: unhandled error on instance "
                    "'%s' (id=%s)", instance.name, instance.id,
                )

    # ------------------------------------------------------------------
    # Order fetching
    # ------------------------------------------------------------------

    def _fetch_and_process_orders(self):
        """
        Fetch unfulfilled eBay orders created since the last sync, paginate
        through the results, and call sale.order.process_ebay_order() for
        each one.  Updates last_order_sync on success.
        """
        self.ensure_one()
        client = EbayApiClient(self)

        # Date lower bound: last sync minus a 5-minute overlap to catch
        # any orders that landed in the gap between polls.
        if self.last_order_sync:
            since = self.last_order_sync - timedelta(minutes=5)
        else:
            since = datetime.utcnow() - timedelta(hours=24)

        since_str  = since.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        filter_str = (
            f"creationdate:[{since_str}..] "
            f"orderfulfillmentstatus:{{NOT_STARTED|IN_PROGRESS}}"
        )

        limit  = 50
        offset = 0
        SaleOrder = self.env['sale.order']

        while True:
            try:
                response = client.get(
                    '/sell/fulfillment/v1/order',
                    filter=filter_str,
                    limit=limit,
                    offset=offset,
                )
            except RequestException:
                _logger.exception(
                    "eBay: HTTP error fetching order page (offset=%s) for "
                    "instance '%s'", offset, self.name,
                )
                break

            orders = response.get('orders', [])
            if not orders:
                break

            for order_data in orders:
                try:
                    SaleOrder.sudo().process_ebay_order(self, order_data)
                except Exception:
                    _logger.exception(
                        "eBay: failed to process order %s for instance '%s'",
                        order_data.get('orderId'), self.name,
                    )

            total   = int(response.get('total', 0))
            offset += limit
            if offset >= total:
                break

        self.sudo().write({'last_order_sync': fields.Datetime.now()})

    def _fetch_single_order(self, ebay_order_id):
        """
        Fetch one eBay order by ID and pass it to process_ebay_order.
        Used by the webhook controller for real-time event handling.
        """
        self.ensure_one()
        client = EbayApiClient(self)
        order_data = client.get(f'/sell/fulfillment/v1/order/{ebay_order_id}')
        if order_data:
            self.env['sale.order'].sudo().process_ebay_order(self, order_data)

    # ------------------------------------------------------------------
    # Inventory push (called by product.py and the cron)
    # ------------------------------------------------------------------

    def _push_all_inventory(self):
        """
        Push Odoo stock levels to eBay for every active sync-enabled
        mapping on this instance.  Batches requests in groups of 25
        (eBay bulk_update_price_quantity limit).
        """
        self.ensure_one()
        mappings = self.env['ebay.product.mapping'].search([
            ('instance_id', '=', self.id),
            ('sync_inventory', '=', True),
            ('active', '=', True),
            ('listing_status', 'in', ['active', 'out_of_stock']),
        ])
        if not mappings:
            return

        client = EbayApiClient(self)
        now    = fields.Datetime.now()

        for i in range(0, len(mappings), 25):
            batch = mappings[i:i + 25]
            self._push_inventory_batch(client, batch, now)

    def _push_inventory_batch(self, client, mappings, now=None):
        """
        Push a batch (≤ 25) of product mappings to the eBay
        bulk_update_price_quantity endpoint.

        :param client:   EbayApiClient instance.
        :param mappings: ebay.product.mapping recordset (≤ 25 records).
        :param now:      Optional datetime stamp for last_inventory_sync.
        """
        if now is None:
            now = fields.Datetime.now()

        requests_payload = []
        mapping_by_sku   = {}

        for mapping in mappings:
            product = mapping.odoo_product_id
            if not product:
                continue

            sku = (
                mapping.ebay_sku
                or product.default_code
                or str(product.id)
            )
            qty = int(max(
                0,
                product.with_context(
                    warehouse=self.warehouse_id.id if self.warehouse_id else False
                ).qty_available,
            ))

            requests_payload.append({
                'sku': sku,
                'shipToLocationAvailability': {'quantity': qty},
            })
            mapping_by_sku[sku] = mapping

        if not requests_payload:
            return

        try:
            client.post(
                '/sell/inventory/v1/bulk_update_price_quantity',
                {'requests': requests_payload},
            )
            for sku, mapping in mapping_by_sku.items():
                product = mapping.odoo_product_id
                qty = int(max(0, product.qty_available)) if product else 0
                mapping.sudo().write({
                    'ebay_quantity':       qty,
                    'last_inventory_sync': now,
                    'sync_error_message':  False,
                })
        except Exception as exc:
            err_msg = str(exc)[:500]
            _logger.error(
                "eBay: bulk inventory update failed for instance '%s': %s",
                self.name, err_msg,
            )
            for mapping in mappings:
                mapping.sudo().write({'sync_error_message': err_msg})

    def _push_fulfillment(self, ebay_order_id, line_items, carrier_name,
                          tracking_number, shipped_date=None):
        """
        POST a shipping fulfillment record to eBay for the given order.

        :param ebay_order_id:   eBay order ID string.
        :param line_items:      List of dicts: [{'lineItemId': '...', 'quantity': N}].
        :param carrier_name:    Odoo carrier name (mapped to eBay carrier code).
        :param tracking_number: Carrier tracking reference string.
        :param shipped_date:    datetime; defaults to now.
        """
        self.ensure_one()
        if not line_items:
            _logger.warning(
                "eBay _push_fulfillment called with no line items for order %s",
                ebay_order_id,
            )
            return

        if shipped_date is None:
            shipped_date = datetime.utcnow()

        payload = {
            'lineItems':          line_items,
            'shippedDate':        shipped_date.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'shippingCarrierCode': _carrier_to_ebay_code(carrier_name),
        }
        if tracking_number:
            payload['trackingNumber'] = tracking_number

        client = EbayApiClient(self)
        try:
            client.post(
                f'/sell/fulfillment/v1/order/{ebay_order_id}/shipping_fulfillment',
                payload,
            )
            _logger.info(
                "eBay: fulfillment posted for order %s (carrier=%s, tracking=%s)",
                ebay_order_id, payload['shippingCarrierCode'], tracking_number,
            )
        except Exception as exc:
            _logger.error(
                "eBay: failed to post fulfillment for order %s on instance '%s': %s",
                ebay_order_id, self.name, exc,
            )
            raise

    # ------------------------------------------------------------------
    # Listing pull (batch cron + single-item webhook helper)
    # ------------------------------------------------------------------

    def _cron_pull_listings(self):
        """
        Called by ir.cron.  Iterates all active instances with listing-sync
        enabled and pulls the full eBay inventory catalogue into Odoo mappings.
        """
        instances = self.search([
            ('active', '=', True),
            ('connection_status', '=', 'connected'),
            ('enable_listing_sync', '=', True),
        ])
        for instance in instances:
            try:
                instance._pull_and_sync_listings()
            except Exception:
                _logger.exception(
                    "eBay _cron_pull_listings: unhandled error on instance "
                    "'%s' (id=%s)", instance.name, instance.id,
                )

    def _pull_and_sync_listings(self):
        """
        Full catalogue pull for this instance.

        Strategy (avoids N+1 API calls):
          1. Pre-fetch ALL offers in one paginated pass → dict {sku: offer_data}
          2. Paginate GET /sell/inventory/v1/inventory_item
          3. For each item call Mapping._create_or_sync_from_ebay_item()
        """
        self.ensure_one()
        client  = EbayApiClient(self)
        Mapping = self.env['ebay.product.mapping']

        # Step 1: pre-fetch all offers indexed by SKU
        offers_by_sku = self._fetch_all_offers(client)

        # Step 2: page through inventory items
        limit  = 25
        offset = 0

        while True:
            try:
                response = client.get(
                    '/sell/inventory/v1/inventory_item',
                    limit=limit,
                    offset=offset,
                )
            except RequestException:
                _logger.exception(
                    "eBay: HTTP error fetching inventory items (offset=%s) "
                    "for instance '%s'", offset, self.name,
                )
                break

            items = response.get('inventoryItems', [])
            if not items:
                break

            for item_data in items:
                sku        = item_data.get('sku', '')
                offer_data = offers_by_sku.get(sku, {})
                try:
                    Mapping.sudo()._create_or_sync_from_ebay_item(
                        self, item_data, offer_data
                    )
                except Exception:
                    _logger.exception(
                        "eBay: failed to sync inventory item SKU '%s' for "
                        "instance '%s'", sku, self.name,
                    )

            total   = int(response.get('total', 0))
            offset += limit
            if offset >= total:
                break

        self.sudo().write({'last_listing_pull_sync': fields.Datetime.now()})
        _logger.info(
            "eBay: listing pull complete for instance '%s'", self.name
        )

    def _fetch_all_offers(self, client=None):
        """
        Page through GET /sell/inventory/v1/offer and return a dict mapping
        each SKU to its offer dict.  Used to avoid per-item API calls.

        :param client: Optional EbayApiClient; creates one if not provided.
        :returns:      Dict {sku: offer_dict}
        """
        self.ensure_one()
        if client is None:
            client = EbayApiClient(self)

        offers_by_sku = {}
        limit         = 100
        offset        = 0

        while True:
            try:
                response = client.get(
                    '/sell/inventory/v1/offer',
                    limit=limit,
                    offset=offset,
                )
            except RequestException:
                _logger.exception(
                    "eBay: HTTP error fetching offers (offset=%s) for "
                    "instance '%s'", offset, self.name,
                )
                break

            offers = response.get('offers', [])
            for offer in offers:
                sku = offer.get('sku', '')
                if sku:
                    offers_by_sku[sku] = offer

            total   = int(response.get('total', 0))
            offset += limit
            if offset >= total:
                break

        return offers_by_sku

    def _fetch_and_sync_single_item(self, sku):
        """
        Fetch one inventory item (and its offer) from eBay by SKU and
        sync it into Odoo.  Used by the webhook controller for real-time
        listing-change events.
        """
        self.ensure_one()
        client = EbayApiClient(self)

        try:
            item_data  = client.get(f'/sell/inventory/v1/inventory_item/{sku}')
            offer_resp = client.get('/sell/inventory/v1/offer', sku=sku)
            offers     = offer_resp.get('offers', [])
            offer_data = offers[0] if offers else {}
        except RequestException:
            _logger.exception(
                "eBay: HTTP error fetching item/offer for SKU '%s' on "
                "instance '%s'", sku, self.name,
            )
            return

        self.env['ebay.product.mapping'].sudo()._create_or_sync_from_ebay_item(
            self, item_data, offer_data
        )

    # ------------------------------------------------------------------
    # Order export (batch cron)
    # ------------------------------------------------------------------

    def _cron_export_orders(self):
        """
        Called by ir.cron.  Finds all confirmed sale orders that are marked
        for eBay export (ebay_export_status == 'pending') and pushes them.
        """
        instances = self.search([
            ('active', '=', True),
            ('connection_status', '=', 'connected'),
            ('enable_order_export', '=', True),
        ])
        if not instances:
            return

        pending_orders = self.env['sale.order'].search([
            ('ebay_export_status', '=', 'pending'),
            ('state', 'in', ['sale', 'done']),
        ])

        for order in pending_orders:
            # Use the order's assigned instance, or fall back to the first
            # connected instance that has order export enabled
            instance = order.ebay_instance_id
            if not instance or instance not in instances:
                instance = instances[:1]
            try:
                order.sudo()._do_export_to_ebay(instance)
            except Exception:
                _logger.exception(
                    "eBay _cron_export_orders: failed to export order %s",
                    order.name,
                )
                order.sudo().write({'ebay_export_status': 'error'})
