# -*- coding: utf-8 -*-
"""
stock.picking extension — eBay Fulfillment push
================================================
When an outbound delivery order is validated (state → done), if it is linked
to a sale order that originated from eBay, the connector POSTs a shipping
fulfillment record to the eBay Shipping Fulfillment API:

    POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment

The payload carries the eBay line-item IDs (stored on sale.order.line), the
carrier tracking reference, and a carrier code mapped to eBay's enum.

Failures are **logged but never re-raised** so a transient eBay API outage
cannot block a warehouse operator from completing their stock validation.
"""

import logging
from datetime import datetime

from odoo import models, _

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # ------------------------------------------------------------------
    # Override _action_done — the definitive "picking is now done" hook
    # ------------------------------------------------------------------

    def _action_done(self):
        """
        Run the standard validation, then push fulfillment to eBay for any
        outbound picking that is now linked to an eBay sale order.
        """
        result = super()._action_done()

        # Only process pickings that are now genuinely done and are
        # outbound deliveries.  _action_done() may be called on a mixed
        # recordset; filter strictly.
        done_deliveries = self.filtered(
            lambda p: p.state == 'done' and p.picking_type_code == 'outgoing'
        )
        for picking in done_deliveries:
            try:
                picking._push_ebay_fulfillment()
            except Exception:
                # Log but never raise — stock state is already committed.
                _logger.exception(
                    "eBay fulfillment push failed for picking %s (id=%s); "
                    "stock validation was NOT rolled back.",
                    picking.name, picking.id,
                )

        return result

    # ------------------------------------------------------------------
    # eBay fulfillment push
    # ------------------------------------------------------------------

    def _push_ebay_fulfillment(self):
        """
        Build and POST the eBay shipping fulfillment payload for this picking.

        Requires:
          • picking.sale_id.ebay_order_id      — eBay order identifier
          • picking.sale_id.ebay_instance_id   — connected eBay instance
          • picking.sale_id.order_line
              └─ ebay_line_item_id             — eBay lineItemId per line
          • picking.carrier_tracking_ref       — from the delivery module
          • picking.carrier_id.name            — mapped to eBay carrier code
        """
        self.ensure_one()

        sale = self.sale_id
        if not sale or not sale.ebay_order_id:
            return  # Not an eBay-originated delivery

        instance = sale.ebay_instance_id
        if not instance:
            _logger.warning(
                "Picking %s is for eBay order %s but has no ebay_instance_id "
                "on the sale order — fulfillment skipped.",
                self.name, sale.ebay_order_id,
            )
            return

        if instance.connection_status != 'connected':
            _logger.warning(
                "eBay instance '%s' is not connected; fulfillment for order "
                "%s skipped.", instance.name, sale.ebay_order_id,
            )
            return

        # --- Collect eBay line items for this picking --------------------
        # We include every sale line that has an eBay line item ID and whose
        # product appears in the done move lines of this picking.
        picked_products = self.move_line_ids.filtered(
            lambda ml: ml.qty_done > 0
        ).mapped('product_id')

        line_items = []
        for sol in sale.order_line:
            if not sol.ebay_line_item_id:
                continue
            if sol.product_id not in picked_products:
                continue
            line_items.append({
                'lineItemId': sol.ebay_line_item_id,
                'quantity':   int(sol.product_uom_qty),
            })

        if not line_items:
            _logger.info(
                "Picking %s: no eBay line items matched picked products; "
                "fulfillment push skipped for order %s.",
                self.name, sale.ebay_order_id,
            )
            return

        # --- Carrier info ------------------------------------------------
        carrier_name     = (
            self.carrier_id.name if self.carrier_id else ''
        )
        tracking_number  = self.carrier_tracking_ref or ''
        shipped_date     = (
            self.date_done.replace(tzinfo=None)
            if self.date_done
            else datetime.utcnow()
        )

        # --- Delegate to instance (which owns the EbayApiClient) ---------
        instance.sudo()._push_fulfillment(
            ebay_order_id=sale.ebay_order_id,
            line_items=line_items,
            carrier_name=carrier_name,
            tracking_number=tracking_number,
            shipped_date=shipped_date,
        )

        # --- Update the ebay.order tracker -------------------------------
        tracker = self.env['ebay.order'].search([
            ('ebay_order_id', '=', sale.ebay_order_id),
            ('instance_id',   '=', instance.id),
        ], limit=1)
        if tracker:
            tracker.sudo().write({'fulfillment_status': 'fulfilled'})
