# -*- coding: utf-8 -*-
"""
eBay Listing Management
=======================
Extends ebay.product.mapping with eBay Inventory API listing fields and
the full bidirectional push/pull workflow.

Push (Odoo → eBay)
    action_publish_to_ebay()      — 3-step: PUT inventory_item → POST/PUT offer
                                    → POST publish
    action_update_ebay_listing()  — re-pushes title, price, qty to a live listing
    action_withdraw_from_ebay()   — ends the listing without deleting the inventory
                                    item or the mapping record

Pull (eBay → Odoo)
    action_pull_from_ebay()            — refresh a single mapping from the eBay API
    _create_or_sync_from_ebay_item()   — @api.model factory used by the batch cron
                                         and the webhook controller
"""

import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


_MARKETPLACE_SELECTION = [
    ('EBAY_US',        'eBay United States'),
    ('EBAY_GB',        'eBay United Kingdom'),
    ('EBAY_DE',        'eBay Germany'),
    ('EBAY_FR',        'eBay France'),
    ('EBAY_IT',        'eBay Italy'),
    ('EBAY_ES',        'eBay Spain'),
    ('EBAY_CA',        'eBay Canada'),
    ('EBAY_AU',        'eBay Australia'),
    ('EBAY_AT',        'eBay Austria'),
    ('EBAY_BE',        'eBay Belgium'),
    ('EBAY_NL',        'eBay Netherlands'),
    ('EBAY_PL',        'eBay Poland'),
    ('EBAY_CH',        'eBay Switzerland'),
    ('EBAY_IE',        'eBay Ireland'),
    ('EBAY_MOTORS_US', 'eBay Motors'),
]

_CONDITION_SELECTION = [
    ('NEW',                       'New'),
    ('LIKE_NEW',                  'Like New / Open Box'),
    ('NEW_OTHER',                 'New (Other)'),
    ('NEW_WITH_DEFECTS',          'New with Defects'),
    ('CERTIFIED_REFURBISHED',     'Certified Refurbished'),
    ('EXCELLENT_REFURBISHED',     'Excellent Refurbished'),
    ('VERY_GOOD_REFURBISHED',     'Very Good Refurbished'),
    ('GOOD_REFURBISHED',          'Good Refurbished'),
    ('SELLER_REFURBISHED',        'Seller Refurbished'),
    ('USED_EXCELLENT',            'Used — Excellent'),
    ('USED_VERY_GOOD',            'Used — Very Good'),
    ('USED_GOOD',                 'Used — Good'),
    ('USED_ACCEPTABLE',           'Used — Acceptable'),
    ('FOR_PARTS_OR_NOT_WORKING',  'For Parts or Not Working'),
]

_VALID_CONDITIONS = {k for k, _ in _CONDITION_SELECTION}
_VALID_MARKETPLACES = {k for k, _ in _MARKETPLACE_SELECTION}


class EbayListingMixin(models.Model):
    """
    Extends ebay.product.mapping with eBay listing management fields
    and the full push/pull workflow.
    """
    _inherit = 'ebay.product.mapping'

    # ------------------------------------------------------------------
    # Listing identity
    # ------------------------------------------------------------------
    offer_id = fields.Char(
        string='eBay Offer ID',
        readonly=True,
        copy=False,
        index=True,
        help='The offerId returned by the eBay Inventory API after the first '
             'successful publish call.',
    )
    marketplace_id = fields.Selection(
        selection=_MARKETPLACE_SELECTION,
        string='Marketplace',
        default='EBAY_US',
        required=True,
        help='The eBay marketplace on which this listing is (or will be) published.',
    )

    # ------------------------------------------------------------------
    # Listing content
    # ------------------------------------------------------------------
    ebay_title = fields.Char(
        string='eBay Title',
        size=80,
        help='Listing title displayed on eBay (max 80 characters).',
    )
    ebay_description = fields.Text(
        string='eBay Description',
        help='Listing description sent to eBay. HTML tags are accepted.',
    )
    ebay_condition = fields.Selection(
        selection=_CONDITION_SELECTION,
        string='Item Condition',
        default='NEW',
        help='eBay item condition code.',
    )
    ebay_category_id = fields.Char(
        string='eBay Category ID',
        help='Numeric leaf-category ID from the eBay category tree. '
             'Required for publishing. Look up IDs in eBay Seller Hub.',
    )

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------
    ebay_price = fields.Monetary(
        string='eBay Price',
        currency_field='ebay_currency_id',
        help='Buy-It-Now price on eBay.',
    )
    ebay_currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
        help='Currency for the eBay listing price.',
    )

    # ------------------------------------------------------------------
    # Business policies (IDs from eBay Seller Hub → Account → Policies)
    # ------------------------------------------------------------------
    fulfillment_policy_id = fields.Char(
        string='Fulfillment Policy ID',
        help='eBay fulfillment (shipping) policy ID. Falls back to the '
             'instance-level default if left blank.',
    )
    payment_policy_id = fields.Char(
        string='Payment Policy ID',
        help='eBay payment policy ID. Falls back to the instance-level '
             'default if left blank.',
    )
    return_policy_id = fields.Char(
        string='Return Policy ID',
        help='eBay return policy ID. Falls back to the instance-level '
             'default if left blank.',
    )

    # ------------------------------------------------------------------
    # Sync metadata
    # ------------------------------------------------------------------
    last_listing_sync = fields.Datetime(
        string='Last Listing Sync',
        readonly=True,
        copy=False,
        help='UTC timestamp of the most recent successful push or pull.',
    )

    # ==================================================================
    # Push: Odoo → eBay
    # ==================================================================

    def action_publish_to_ebay(self):
        """
        3-step publish flow for each selected mapping:
          1. PUT /sell/inventory/v1/inventory_item/{sku}
          2. POST /sell/inventory/v1/offer  (or PUT if offer_id already set)
          3. POST /sell/inventory/v1/offer/{offerId}/publish
        Stores the returned listingId and offerId on the mapping record.
        """
        for mapping in self:
            try:
                mapping._publish_single()
            except UserError:
                raise
            except Exception as exc:
                mapping.sudo().write({
                    'listing_status':     'ended',
                    'sync_error_message': str(exc)[:500],
                })
                _logger.error(
                    "eBay publish failed for mapping %s (SKU=%s): %s",
                    mapping.id, mapping.ebay_sku, exc,
                )
                raise UserError(
                    _("eBay publish failed for '%s':\n%s")
                    % (mapping.display_name, exc)
                ) from exc

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _("Published to eBay"),
                'message': _("%d listing(s) published successfully.") % len(self),
                'type':    'success',
                'sticky':  False,
            },
        }

    def _publish_single(self):
        """
        Worker for action_publish_to_ebay.  Operates on a single record.
        Raises UserError on validation failures; raises requests.HTTPError on
        API failures (caught and re-wrapped by action_publish_to_ebay).
        """
        self.ensure_one()
        client   = self.instance_id._get_api_client()
        product  = self.odoo_product_id
        instance = self.instance_id

        if not product:
            raise UserError(_("No Odoo product is linked to this mapping."))

        sku         = self.ebay_sku or product.default_code or str(product.id)
        title       = (self.ebay_title or product.name or sku)[:80]
        description = (
            self.ebay_description
            or (product.description_sale if product.description_sale else '')
            or product.name
            or sku
        )
        condition = self.ebay_condition or 'NEW'

        # Current available quantity in the instance's warehouse
        qty = int(max(0, product.with_context(
            warehouse=instance.warehouse_id.id if instance.warehouse_id else False
        ).qty_available))

        # ------ Step 1: PUT inventory_item --------------------------------
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        product_payload = {
            'title':       title,
            'description': description,
        }
        if product.image_1920:
            image_url = f"{base_url}/web/image/product.product/{product.id}/image_1920"
            product_payload['imageUrls'] = [image_url]

        client.put(
            f'/sell/inventory/v1/inventory_item/{sku}',
            {
                'product': product_payload,
                'condition': condition,
                'availability': {
                    'shipToLocationAvailability': {'quantity': qty},
                },
            },
        )

        # ------ Step 2: POST / PUT offer ----------------------------------
        currency_code = self.ebay_currency_id.name if self.ebay_currency_id else 'USD'
        price         = self.ebay_price or product.list_price or 0.0

        offer_payload = {
            'sku':           sku,
            'marketplaceId': self.marketplace_id or 'EBAY_US',
            'format':        'FIXED_PRICE',
            'availableQuantity': qty,
            'merchantLocationKey': instance.merchant_location_key or 'default',
            'pricingSummary': {
                'price': {
                    'value':    str(round(price, 2)),
                    'currency': currency_code,
                },
            },
        }
        if self.ebay_category_id:
            offer_payload['categoryId'] = self.ebay_category_id

        # Merge per-listing policies with instance-level defaults
        policies = {}
        _fill_policy(policies, 'fulfillmentPolicyId',
                     self.fulfillment_policy_id,
                     instance.default_fulfillment_policy_id)
        _fill_policy(policies, 'paymentPolicyId',
                     self.payment_policy_id,
                     instance.default_payment_policy_id)
        _fill_policy(policies, 'returnPolicyId',
                     self.return_policy_id,
                     instance.default_return_policy_id)
        if policies:
            offer_payload['listingPolicies'] = policies

        if self.offer_id:
            client.put(f'/sell/inventory/v1/offer/{self.offer_id}', offer_payload)
            offer_id = self.offer_id
        else:
            resp     = client.post('/sell/inventory/v1/offer', offer_payload)
            offer_id = resp.get('offerId', '')
            if not offer_id:
                raise UserError(
                    _("eBay did not return an offerId for SKU '%s'. "
                      "Check that your policy IDs and category ID are correct.")
                    % sku
                )

        # ------ Step 3: Publish ------------------------------------------
        pub_resp   = client.post(
            f'/sell/inventory/v1/offer/{offer_id}/publish', {}
        )
        listing_id = pub_resp.get('listingId', '')

        self.sudo().write({
            'offer_id':           offer_id,
            'ebay_item_id':       listing_id or self.ebay_item_id or sku,
            'ebay_sku':           sku,
            'listing_status':     'active',
            'ebay_quantity':      qty,
            'last_listing_sync':  fields.Datetime.now(),
            'sync_error_message': False,
        })
        _logger.info(
            "eBay: published SKU '%s' → listing %s (offer %s) on '%s'",
            sku, listing_id, offer_id, instance.name,
        )

    def action_update_ebay_listing(self):
        """
        Re-push title, description, condition, price, and quantity to
        already-published listings (offer must exist).
        """
        for mapping in self:
            if not mapping.offer_id:
                if self.env.context.get('ebay_no_raise'):
                    continue
                raise UserError(
                    _("'%s' has no eBay Offer ID — publish to eBay first.")
                    % mapping.display_name
                )
            try:
                mapping._update_single()
            except UserError as e:
                if self.env.context.get('ebay_no_raise'):
                    _logger.warning("eBay UserError suppressed during instant sync: %s", e)
                    continue
                raise
            except Exception as exc:
                mapping.sudo().write({'sync_error_message': str(exc)[:500]})
                if self.env.context.get('ebay_no_raise'):
                    _logger.error("eBay Exception suppressed during instant sync: %s", exc)
                    continue
                raise UserError(
                    _("eBay update failed for '%s':\n%s")
                    % (mapping.display_name, exc)
                ) from exc

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _("Listing Updated"),
                'message': _("%d listing(s) updated on eBay.") % len(self),
                'type':    'success',
                'sticky':  False,
            },
        }

    def _update_single(self):
        """Worker for action_update_ebay_listing."""
        self.ensure_one()
        client   = self.instance_id._get_api_client()
        product  = self.odoo_product_id
        instance = self.instance_id

        sku         = self.ebay_sku or (product.default_code if product else '') or str(self.id)
        title       = (self.ebay_title or (product.name if product else sku) or sku)[:80]
        description = (
            self.ebay_description
            or (product.description_sale or product.name if product else title)
            or title
        )
        condition = self.ebay_condition or 'NEW'
        qty       = int(max(0, product.with_context(
            warehouse=instance.warehouse_id.id if instance.warehouse_id else False
        ).qty_available)) if product else 0

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        product_payload = {
            'title':       title,
            'description': description,
        }
        if product and product.image_1920:
            image_url = f"{base_url}/web/image/product.product/{product.id}/image_1920"
            product_payload['imageUrls'] = [image_url]

        client.put(
            f'/sell/inventory/v1/inventory_item/{sku}',
            {
                'product':   product_payload,
                'condition': condition,
                'availability': {
                    'shipToLocationAvailability': {'quantity': qty},
                },
            },
        )

        currency_code = self.ebay_currency_id.name if self.ebay_currency_id else 'USD'
        price         = self.ebay_price or (product.list_price if product else 0.0) or 0.0

        offer_payload = {
            'availableQuantity': qty,
            'merchantLocationKey': instance.merchant_location_key or 'default',
            'pricingSummary': {
                'price': {'value': str(round(price, 2)), 'currency': currency_code},
            },
        }
        if self.ebay_category_id:
            offer_payload['categoryId'] = self.ebay_category_id

        policies = {}
        _fill_policy(policies, 'fulfillmentPolicyId',
                     self.fulfillment_policy_id,
                     instance.default_fulfillment_policy_id)
        _fill_policy(policies, 'paymentPolicyId',
                     self.payment_policy_id,
                     instance.default_payment_policy_id)
        _fill_policy(policies, 'returnPolicyId',
                     self.return_policy_id,
                     instance.default_return_policy_id)
        if policies:
            offer_payload['listingPolicies'] = policies

        client.put(f'/sell/inventory/v1/offer/{self.offer_id}', offer_payload)

        self.sudo().write({
            'ebay_quantity':      qty,
            'last_listing_sync':  fields.Datetime.now(),
            'sync_error_message': False,
        })

    def action_withdraw_from_ebay(self):
        """
        Withdraw the eBay offer (end the listing) without deleting the
        inventory item record or the Odoo mapping.
        """
        for mapping in self:
            if not mapping.offer_id:
                raise UserError(
                    _("'%s' has no eBay Offer ID to withdraw.")
                    % mapping.display_name
                )
            try:
                client = mapping.instance_id._get_api_client()
                client.post(
                    f'/sell/inventory/v1/offer/{mapping.offer_id}/withdraw', {}
                )
                mapping.sudo().write({
                    'listing_status':     'ended',
                    'last_listing_sync':  fields.Datetime.now(),
                    'sync_error_message': False,
                })
                _logger.info(
                    "eBay: offer %s withdrawn (mapping %s)", mapping.offer_id, mapping.id
                )
            except UserError:
                raise
            except Exception as exc:
                mapping.sudo().write({'sync_error_message': str(exc)[:500]})
                raise UserError(
                    _("eBay withdraw failed for '%s':\n%s")
                    % (mapping.display_name, exc)
                ) from exc

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _("Listing Withdrawn"),
                'message': _("%d listing(s) withdrawn from eBay.") % len(self),
                'type':    'warning',
                'sticky':  False,
            },
        }

    # ==================================================================
    # Pull: eBay → Odoo
    # ==================================================================

    def action_pull_from_ebay(self):
        """
        Fetch the current state of each selected listing from the eBay
        Inventory API and update the mapping record in Odoo.
        """
        for mapping in self:
            try:
                mapping._pull_single()
            except UserError:
                raise
            except Exception as exc:
                mapping.sudo().write({'sync_error_message': str(exc)[:500]})
                raise UserError(
                    _("eBay pull failed for '%s':\n%s")
                    % (mapping.display_name, exc)
                ) from exc

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _("Pulled from eBay"),
                'message': _("%d listing(s) refreshed from eBay.") % len(self),
                'type':    'success',
                'sticky':  False,
            },
        }

    def _pull_single(self):
        """Fetch this mapping's data from eBay and sync it to the record."""
        self.ensure_one()
        client  = self.instance_id._get_api_client()
        product = self.odoo_product_id
        sku     = self.ebay_sku or (product.default_code if product else '') or str(self.id)

        item_data  = client.get(f'/sell/inventory/v1/inventory_item/{sku}')
        offer_resp = client.get('/sell/inventory/v1/offer', sku=sku)
        offers     = offer_resp.get('offers', [])
        offer_data = offers[0] if offers else {}

        self._sync_fields_from_ebay(item_data, offer_data)

    # ==================================================================
    # Factory — called by EbayInstanceApiMethods._pull_and_sync_listings
    # and the webhook controller
    # ==================================================================

    @api.model
    def _create_or_sync_from_ebay_item(self, instance, item_data, offer_data=None):
        """
        Find or create an ebay.product.mapping from eBay item/offer data.

        :param instance:   ebay.instance record.
        :param item_data:  Dict from GET /sell/inventory/v1/inventory_item.
        :param offer_data: Optional offer dict (pre-fetched; None if not available).
        :returns:          The ebay.product.mapping record (may be empty recordset
                           if the item has no SKU or no matchable product).
        """
        if offer_data is None:
            offer_data = {}

        sku = (item_data.get('sku') or '').strip()
        if not sku:
            _logger.warning(
                "eBay _create_or_sync_from_ebay_item: item has no SKU, skipping."
            )
            return self.browse()

        # 1. Try to find by (instance, sku)
        mapping = self.search([
            ('instance_id', '=', instance.id),
            ('ebay_sku', '=', sku),
        ], limit=1)

        # 2. Fall back to listing ID from the offer
        if not mapping:
            listing_id = offer_data.get('listing', {}).get('listingId', '')
            if listing_id:
                mapping = self.search([
                    ('instance_id', '=', instance.id),
                    ('ebay_item_id', '=', listing_id),
                ], limit=1)

        # 3. Resolve or create the Odoo product
        product = self._find_or_create_product_for_sku(sku, item_data)
        if not product:
            _logger.warning(
                "eBay: no product found/created for SKU '%s' — skipping.", sku
            )
            return self.browse()

        if mapping:
            mapping.sudo().write({'odoo_product_id': product.id})
        else:
            listing_id = offer_data.get('listing', {}).get('listingId', '') or sku
            mapping = self.sudo().create({
                'instance_id':     instance.id,
                'odoo_product_id': product.id,
                'ebay_item_id':    listing_id,
                'ebay_sku':        sku,
            })

        mapping._sync_fields_from_ebay(item_data, offer_data)
        return mapping

    @api.model
    def _find_or_create_product_for_sku(self, sku, item_data):
        """
        Return a product.product whose default_code matches sku, creating a
        new product.template (and its single variant) when none is found.
        """
        product = self.env['product.product'].search([
            ('default_code', '=', sku),
            ('active', '=', True),
        ], limit=1)
        if product:
            return product

        title = (item_data.get('product', {}).get('title') or sku)
        tmpl  = self.env['product.template'].sudo().create({
            'name':         title,
            'default_code': sku,
            'type':         'product',
            'sale_ok':      True,
            'purchase_ok':  True,
            'active':       True,
        })
        return tmpl.product_variant_id

    # ==================================================================
    # Internal sync helper
    # ==================================================================

    def _sync_fields_from_ebay(self, item_data, offer_data):
        """
        Update this mapping's listing fields from eBay API response dicts.
        Called by _pull_single and _create_or_sync_from_ebay_item.
        """
        self.ensure_one()
        product_info = item_data.get('product', {})
        avail        = (
            item_data.get('availability', {})
                     .get('shipToLocationAvailability', {})
        )

        qty       = int(avail.get('quantity', 0))
        condition = item_data.get('condition', 'NEW')

        pricing       = offer_data.get('pricingSummary', {}).get('price', {})
        price_val     = float(pricing.get('value', 0.0))
        currency_code = pricing.get('currency', 'USD')
        currency      = self.env['res.currency'].search(
            [('name', '=', currency_code)], limit=1
        )

        offer_status = offer_data.get('status', '')
        listing_id   = offer_data.get('listing', {}).get('listingId', '')
        offer_id     = offer_data.get('offerId', '')
        marketplace  = offer_data.get('marketplaceId', '')
        category_id  = offer_data.get('categoryId', '')

        status_map = {
            'PUBLISHED':    'active',
            'UNPUBLISHED':  'ended',
            'OUT_OF_STOCK': 'out_of_stock',
        }
        listing_status = status_map.get(offer_status, 'unknown')

        policies = offer_data.get('listingPolicies', {})

        vals = {
            'ebay_quantity':      qty,
            'last_listing_sync':  fields.Datetime.now(),
            'sync_error_message': False,
        }

        title = product_info.get('title', '')
        if title:
            vals['ebay_title'] = title[:80]

        if condition in _VALID_CONDITIONS:
            vals['ebay_condition'] = condition

        if price_val:
            vals['ebay_price'] = price_val
        if currency:
            vals['ebay_currency_id'] = currency.id
        if listing_id:
            vals['ebay_item_id'] = listing_id
        if offer_id:
            vals['offer_id'] = offer_id
        if listing_status != 'unknown':
            vals['listing_status'] = listing_status
        if marketplace and marketplace in _VALID_MARKETPLACES:
            vals['marketplace_id'] = marketplace
        if category_id:
            vals['ebay_category_id'] = str(category_id)
        if policies.get('fulfillmentPolicyId'):
            vals['fulfillment_policy_id'] = policies['fulfillmentPolicyId']
        if policies.get('paymentPolicyId'):
            vals['payment_policy_id'] = policies['paymentPolicyId']
        if policies.get('returnPolicyId'):
            vals['return_policy_id'] = policies['returnPolicyId']

        self.sudo().write(vals)


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _fill_policy(policies_dict, key, per_listing_val, instance_default):
    """Set policies_dict[key] from per-listing value, falling back to instance default."""
    val = per_listing_val or instance_default
    if val:
        policies_dict[key] = val
