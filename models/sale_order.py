# -*- coding: utf-8 -*-
"""
sale.order, sale.order.line, and ebay.order extensions
=======================================================

sale.order
    • ebay_order_id — the eBay order ID string (indexed, unique among active orders)
    • ebay_instance_id — the originating instance
    • process_ebay_order() — idempotent factory: create/find the Odoo SO from
      an eBay order dict, match customer by email, match products by SKU,
      add a shipping line, auto-confirm when the eBay payment status is PAID.

sale.order.line
    • ebay_line_item_id — eBay lineItemId stored so stock.picking can build the
      fulfillment payload without an extra API round-trip.

ebay.order (thin extension)
    • raw_json — stores the original eBay JSON so the full payload is always
      available for debugging or re-processing.
"""

import json
import logging
from datetime import datetime

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ebay.order — raw JSON storage
# ---------------------------------------------------------------------------

class EbayOrderExtension(models.Model):
    _inherit = 'ebay.order'

    raw_json = fields.Text(
        string='eBay Raw JSON',
        help='Original order payload returned by the eBay Fulfillment API.',
    )


# ---------------------------------------------------------------------------
# sale.order.line — eBay line-item reference
# ---------------------------------------------------------------------------

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    ebay_line_item_id = fields.Char(
        string='eBay Line Item ID',
        index=True,
        copy=False,
        help='eBay lineItemId for this order line; used when posting fulfillment '
             'back to the eBay Shipping Fulfillment API.',
    )


# ---------------------------------------------------------------------------
# sale.order — eBay identity fields + order processor
# ---------------------------------------------------------------------------

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    ebay_order_id = fields.Char(
        string='eBay Order ID',
        index=True,
        copy=False,
        help='eBay orderId for orders imported from eBay.',
    )
    ebay_instance_id = fields.Many2one(
        comodel_name='ebay.instance',
        string='eBay Instance',
        ondelete='set null',
        copy=False,
        index=True,
        help='The eBay account this order was imported from.',
    )

    # ------------------------------------------------------------------
    # Smart-button action: open linked ebay.order record
    # ------------------------------------------------------------------

    def action_view_ebay_order(self):
        """Open the ebay.order record linked to this sale order."""
        self.ensure_one()
        tracker = self.env['ebay.order'].search([
            ('ebay_order_id', '=', self.ebay_order_id),
        ], limit=1)
        if not tracker:
            return {'type': 'ir.actions.act_window_close'}
        return {
            'type':      'ir.actions.act_window',
            'res_model': 'ebay.order',
            'res_id':    tracker.id,
            'view_mode': 'form',
            'target':    'current',
        }

    # ------------------------------------------------------------------
    # Main entry point: create or retrieve the Odoo SO for an eBay order
    # ------------------------------------------------------------------

    @api.model
    def process_ebay_order(self, instance, order_data):
        """
        Idempotent factory method.  Creates a sale.order (and the associated
        ebay.order tracker) from an eBay Fulfillment API order dict.

        :param instance:    ebay.instance record.
        :param order_data:  Parsed JSON dict from GET /sell/fulfillment/v1/order/{id}.
        :returns:           The sale.order record (new or existing).
        """
        ebay_order_id = order_data.get('orderId', '').strip()
        if not ebay_order_id:
            raise UserError(_("eBay order data has no orderId field."))

        # --- Idempotency: return existing order without re-processing ---
        existing_so = self.search(
            [('ebay_order_id', '=', ebay_order_id)], limit=1
        )
        if existing_so:
            _logger.info(
                "eBay order %s already imported as %s — skipping",
                ebay_order_id, existing_so.name,
            )
            return existing_so

        # --- Resolve or create the customer partner ----------------------
        partner, partner_shipping = self._get_or_create_ebay_partners(order_data)

        # --- Currency ----------------------------------------------------
        pricing   = order_data.get('pricingSummary', {})
        total_obj = pricing.get('total', {})
        currency_code = total_obj.get('currency', 'USD')
        currency = self.env['res.currency'].search(
            [('name', '=', currency_code)], limit=1
        )
        if not currency:
            _logger.warning(
                "eBay order %s: currency %s not found; falling back to "
                "company currency.", ebay_order_id, currency_code,
            )
            currency = instance.company_id.currency_id or self.env.company.currency_id

        # --- Warehouse ---------------------------------------------------
        warehouse = instance.warehouse_id or self.env['stock.warehouse'].search(
            [('company_id', '=', (instance.company_id or self.env.company).id)],
            limit=1,
        )

        # --- Build sale.order values ------------------------------------
        order_vals = {
            'partner_id':          partner.id,
            'partner_shipping_id': partner_shipping.id,
            'currency_id':         currency.id,
            'company_id':          (instance.company_id or self.env.company).id,
            'warehouse_id':        warehouse.id if warehouse else False,
            'ebay_order_id':       ebay_order_id,
            'ebay_instance_id':    instance.id,
            'origin':              f"eBay/{ebay_order_id}",
            'note':                (
                f"eBay buyer: {order_data.get('buyer', {}).get('username', '')}\n"
                f"eBay fulfillment status: "
                f"{order_data.get('orderFulfillmentStatus', '')}"
            ),
            'order_line': [],
        }

        # --- Build order lines ------------------------------------------
        order_lines = self._build_ebay_order_lines(order_data, instance)
        order_vals['order_line'] = order_lines

        # --- Create the sale.order and the ebay.order tracker -----------
        order = self.create(order_vals)

        self._create_or_update_ebay_order_tracker(
            order, instance, order_data, currency
        )

        # --- Auto-confirm when eBay reports the order as paid -----------
        payment_status = order_data.get('orderPaymentStatus', '')
        if payment_status == 'PAID':
            try:
                order.with_context(send_email=False).action_confirm()
            except Exception:
                _logger.exception(
                    "eBay: could not auto-confirm SO %s for eBay order %s",
                    order.name, ebay_order_id,
                )

        _logger.info(
            "eBay order %s imported as %s (payment=%s)",
            ebay_order_id, order.name, payment_status,
        )
        return order

    # ------------------------------------------------------------------
    # Partner resolution / creation
    # ------------------------------------------------------------------

    @api.model
    def _get_or_create_ebay_partners(self, order_data):
        """
        Return (invoice_partner, shipping_partner).  Both may be the same
        record when ship-to and bill-to addresses match.

        Lookup priority:
          1. Existing res.partner with matching email.
          2. Create a new partner from the buyer registration address.
        The shipping partner is a 'delivery' child of the invoice partner
        when the fulfilment address differs.
        """
        buyer     = order_data.get('buyer', {})
        reg_addr  = buyer.get('buyerRegistrationAddress', {})
        email     = (reg_addr.get('email') or '').strip().lower()
        full_name = (
            reg_addr.get('fullName')
            or buyer.get('username')
            or 'eBay Buyer'
        )
        phone     = (
            reg_addr.get('primaryPhone', {}).get('phoneNumber') or ''
        )
        bill_addr = reg_addr.get('contactAddress', {})

        # -- Locate or create invoice partner ----------------------------
        invoice_partner = False
        if email:
            invoice_partner = self.env['res.partner'].search(
                [('email', '=', email), ('type', 'in', ['contact', False])],
                limit=1,
            )

        if not invoice_partner:
            invoice_partner = self.env['res.partner'].create(
                self._build_partner_vals(full_name, email, phone, bill_addr,
                                        ptype='contact', rank=1)
            )

        # -- Locate or create shipping (delivery) partner ----------------
        instructions = order_data.get('fulfillmentStartInstructions', [])
        ship_to = {}
        if instructions:
            ship_to = (
                instructions[0]
                .get('shippingStep', {})
                .get('shipTo', {})
            )

        ship_addr     = ship_to.get('contactAddress', {})
        ship_name     = ship_to.get('fullName') or full_name
        ship_email    = (ship_to.get('email') or email).strip().lower()
        ship_phone    = ship_to.get('primaryPhone', {}).get('phoneNumber') or phone

        # Treat as same address when street + zip match
        same_address = (
            (bill_addr.get('addressLine1', '') or '').strip().lower()
            == (ship_addr.get('addressLine1', '') or '').strip().lower()
            and
            (bill_addr.get('postalCode', '') or '').strip()
            == (ship_addr.get('postalCode', '') or '').strip()
        )

        if same_address or not ship_addr:
            shipping_partner = invoice_partner
        else:
            # Look for an existing delivery child partner
            shipping_partner = self.env['res.partner'].search([
                ('parent_id', '=', invoice_partner.id),
                ('type', '=', 'delivery'),
                ('street', '=', ship_addr.get('addressLine1', '')),
                ('zip', '=', ship_addr.get('postalCode', '')),
            ], limit=1)

            if not shipping_partner:
                shipping_partner = self.env['res.partner'].create(
                    self._build_partner_vals(
                        ship_name, ship_email, ship_phone,
                        ship_addr, ptype='delivery', rank=0,
                        parent_id=invoice_partner.id,
                    )
                )

        return invoice_partner, shipping_partner

    @api.model
    def _build_partner_vals(self, name, email, phone, addr_dict,
                            ptype='contact', rank=0, parent_id=False):
        """Construct a res.partner create-vals dict from address components."""
        country_code = (addr_dict.get('countryCode') or '').upper()
        state_code   = (addr_dict.get('stateOrProvince') or '').upper()

        country = (
            self.env['res.country'].search([('code', '=', country_code)], limit=1)
            if country_code else self.env['res.country']
        )
        state = (
            self.env['res.country.state'].search([
                ('country_id', '=', country.id),
                ('code', '=', state_code),
            ], limit=1)
            if country and state_code else self.env['res.country.state']
        )

        return {
            'name':          name,
            'email':         email,
            'phone':         phone,
            'street':        addr_dict.get('addressLine1') or '',
            'street2':       addr_dict.get('addressLine2') or '',
            'city':          addr_dict.get('city') or '',
            'zip':           addr_dict.get('postalCode') or '',
            'country_id':    country.id if country else False,
            'state_id':      state.id if state else False,
            'type':          ptype,
            'customer_rank': rank,
            'parent_id':     parent_id or False,
        }

    # ------------------------------------------------------------------
    # Order lines
    # ------------------------------------------------------------------

    @api.model
    def _build_ebay_order_lines(self, order_data, instance):
        """
        Return a list of (0, 0, vals) tuples for order_line.
        Matches eBay line items to Odoo products by:
          1. ebay.product.mapping.ebay_sku == item SKU
          2. product.product.default_code   == item SKU
        Adds a shipping service line when delivery cost > 0.
        Taxes are cleared on all eBay-sourced lines; eBay collects/remits
        Marketplace Facilitator taxes independently.
        """
        lines = []

        for item in order_data.get('lineItems', []):
            product = self._find_ebay_product(item, instance)
            if not product:
                sku = item.get('sku', '?')
                _logger.warning(
                    "eBay order %s: no Odoo product for SKU '%s' — "
                    "order line skipped.",
                    order_data.get('orderId'), sku,
                )
                continue

            unit_price  = float(
                item.get('lineItemCost', {}).get('value', 0.0)
            )
            quantity    = int(item.get('quantity', 1))
            description = item.get('title') or product.name

            lines.append((0, 0, {
                'product_id':          product.id,
                'name':                description,
                'product_uom_qty':     quantity,
                'price_unit':          unit_price,
                'tax_id':              [(5, 0, 0)],   # clear taxes
                'ebay_line_item_id':   str(item.get('lineItemId', '')),
            }))

        # --- Shipping line ------------------------------------------------
        delivery_cost = float(
            order_data.get('pricingSummary', {})
                      .get('deliveryCost', {})
                      .get('value', 0.0)
        )
        if delivery_cost > 0:
            shipping_product = self._get_or_create_ebay_shipping_product()
            if shipping_product:
                lines.append((0, 0, {
                    'product_id':      shipping_product.id,
                    'name':            _('eBay Shipping'),
                    'product_uom_qty': 1,
                    'price_unit':      delivery_cost,
                    'tax_id':          [(5, 0, 0)],
                    'ebay_line_item_id': '',
                }))

        return lines

    @api.model
    def _find_ebay_product(self, line_item, instance):
        """
        Look up the Odoo product.product for a given eBay line item dict.

        Search order:
          1. ebay.product.mapping (by ebay_sku + instance)
          2. product.product.default_code (internal reference)
        Returns a product.product record or False.
        """
        sku = (line_item.get('sku') or '').strip()

        if sku:
            mapping = self.env['ebay.product.mapping'].search([
                ('instance_id', '=', instance.id),
                ('ebay_sku', '=', sku),
                ('active', '=', True),
            ], limit=1)
            if mapping and mapping.odoo_product_id:
                return mapping.odoo_product_id

            # Fall back to internal reference
            product = self.env['product.product'].search([
                ('default_code', '=', sku),
                ('active', '=', True),
            ], limit=1)
            if product:
                return product

        return False

    @api.model
    def _get_or_create_ebay_shipping_product(self):
        """
        Return (creating if absent) a service product used for eBay shipping lines.
        The product is cached in ir.config_parameter to avoid repeated searches.
        """
        ICP       = self.env['ir.config_parameter'].sudo()
        prod_id   = int(ICP.get_param('ebay_connector.shipping_product_id', 0))
        product   = self.env['product.product'].browse(prod_id).exists()

        if not product:
            product = self.env['product.product'].search([
                ('default_code', '=', 'EBAY_SHIPPING'),
                ('type', '=', 'service'),
            ], limit=1)

        if not product:
            tmpl = self.env['product.template'].create({
                'name':              'eBay Shipping',
                'type':              'service',
                'default_code':      'EBAY_SHIPPING',
                'invoice_policy':    'order',
                'taxes_id':          [(5, 0, 0)],
                'list_price':        0.0,
                'sale_ok':           True,
                'purchase_ok':       False,
            })
            product = tmpl.product_variant_id

        ICP.set_param('ebay_connector.shipping_product_id', product.id)
        return product

    # ------------------------------------------------------------------
    # ebay.order tracker creation / update
    # ------------------------------------------------------------------

    @api.model
    def _create_or_update_ebay_order_tracker(self, sale_order, instance,
                                              order_data, currency):
        """
        Create (or update) the ebay.order record that mirrors this eBay order
        and links it to the newly created sale.order.
        """
        ebay_order_id  = order_data.get('orderId', '')
        payment_status = order_data.get('orderPaymentStatus', '')
        fulfil_status  = order_data.get('orderFulfillmentStatus', 'NOT_STARTED')
        buyer          = order_data.get('buyer', {})
        reg_addr       = buyer.get('buyerRegistrationAddress', {})
        pricing        = order_data.get('pricingSummary', {})
        total_val      = float(pricing.get('total', {}).get('value', 0.0))
        creation_date  = order_data.get('creationDate', '')

        # Map eBay payment status to our order_status selection
        status_map = {
            'PAID':                    'paid',
            'FULLY_REFUNDED':          'cancelled',
            'PARTIALLY_REFUNDED':      'paid',
            'NO_PAYMENT_ACTION_NEEDED': 'paid',
        }
        odoo_order_status = status_map.get(payment_status, 'awaiting_payment')

        # Map eBay fulfillment status
        fulfil_map = {
            'NOT_STARTED': 'not_started',
            'IN_PROGRESS': 'in_progress',
            'FULFILLED':   'fulfilled',
        }
        odoo_fulfil_status = fulfil_map.get(fulfil_status, 'not_started')

        order_date = False
        if creation_date:
            try:
                # eBay timestamps: '2024-01-15T10:00:00.000Z'
                order_date = datetime.strptime(
                    creation_date[:19], '%Y-%m-%dT%H:%M:%S'
                )
            except ValueError:
                pass

        EbayOrder = self.env['ebay.order']
        tracker   = EbayOrder.search([
            ('ebay_order_id', '=', ebay_order_id),
            ('instance_id',   '=', instance.id),
        ], limit=1)

        vals = {
            'odoo_order_id':      sale_order.id,
            'order_status':       odoo_order_status,
            'sync_status':        'synced',
            'total_amount':       total_val,
            'currency_id':        currency.id,
            'buyer_username':     buyer.get('username', ''),
            'buyer_email':        reg_addr.get('email', ''),
            'order_date':         order_date,
            'last_sync_date':     fields.Datetime.now(),
            'sync_error_message': False,
            'fulfillment_status': odoo_fulfil_status,
            'raw_json':           json.dumps(order_data),
        }

        if tracker:
            tracker.write(vals)
        else:
            vals.update({
                'ebay_order_id': ebay_order_id,
                'instance_id':   instance.id,
            })
            EbayOrder.create(vals)
