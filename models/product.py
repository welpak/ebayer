# -*- coding: utf-8 -*-
"""
product.template, product.product, and stock.quant extensions
=============================================================

product.template
    • ebay_listing_count — smart-button count of ebay.product.mapping records.

product.product
    • _push_inventory_to_ebay() — public method that reads qty_available for
      this variant and pushes it to every active, sync-enabled ebay.product.mapping
      via the instance's _push_inventory_batch() helper.

stock.quant (write override)
    • Detects changes to the 'quantity' column (internal locations only) and
      schedules an eBay inventory push for affected products.
    • The push is deferred to after the current database transaction commits
      using self.env.cr.postcommit.add(), so a stock adjustment that rolls
      back never triggers an API call.
    • Failures are caught and logged; they never interrupt the stock operation.
"""

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# product.template — smart button count
# ---------------------------------------------------------------------------

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        result = super(ProductTemplate, self).write(vals)
        trigger_fields = {'name', 'list_price', 'description_sale', 'image_1920'}
        if any(field in vals for field in trigger_fields):
            for template in self:
                template.product_variant_ids._trigger_ebay_sync()
        return result

    ebay_listing_count = fields.Integer(
        string='eBay Listings',
        compute='_compute_ebay_listing_count',
        help='Number of eBay product mappings linked to this template.',
    )

    @api.depends('product_variant_ids')
    def _compute_ebay_listing_count(self):
        """Count active ebay.product.mapping records for each template."""
        mapping_data = self.env['ebay.product.mapping'].read_group(
            domain=[
                ('odoo_product_tmpl_id', 'in', self.ids),
                ('active', '=', True),
            ],
            fields=['odoo_product_tmpl_id'],
            groupby=['odoo_product_tmpl_id'],
        )
        counts = {
            row['odoo_product_tmpl_id'][0]: row['odoo_product_tmpl_id_count']
            for row in mapping_data
        }
        for tmpl in self:
            tmpl.ebay_listing_count = counts.get(tmpl.id, 0)

    def action_view_ebay_listings(self):
        """Open the eBay product mappings for this template."""
        self.ensure_one()
        return {
            'name':     'eBay Listings',
            'type':     'ir.actions.act_window',
            'res_model': 'ebay.product.mapping',
            'view_mode': 'tree,form',
            'domain':   [('odoo_product_tmpl_id', '=', self.id)],
            'context':  {'default_odoo_product_tmpl_id': self.id},
        }


# ---------------------------------------------------------------------------
# product.product — inventory push
# ---------------------------------------------------------------------------

class ProductProduct(models.Model):
    _inherit = 'product.product'

    def write(self, vals):
        result = super(ProductProduct, self).write(vals)
        trigger_fields = {'name', 'list_price', 'description_sale', 'image_1920'}
        if any(field in vals for field in trigger_fields):
            self._trigger_ebay_sync()
        return result

    def _trigger_ebay_sync(self):
        """
        Triggers an immediate update to eBay for the given products, bypassing the queue.
        Used for updating listing details when product fields change.
        """
        mappings = self.env['ebay.product.mapping'].search([
            ('odoo_product_id', 'in', self.ids),
            ('sync_inventory', '=', True),
            ('active', '=', True),
            ('listing_status', 'in', ['active', 'out_of_stock']),
        ])
        if mappings:
            for mapping in mappings:
                try:
                    mapping.with_context(ebay_no_raise=True).action_update_ebay_listing()
                except Exception:
                    _logger.exception(
                        "Failed to update eBay listing for mapping %s during product save",
                        mapping.id
                    )

    def _push_inventory_to_ebay(self):
        """
        Push the current Odoo stock level for each variant in self to all
        active, sync-enabled ebay.product.mapping records.

        Groups mappings by instance so each instance receives a single batched
        bulk_update_price_quantity call (≤ 25 SKUs per request).

        Safe to call on an empty recordset; silently no-ops when no mappings
        are configured for a product.
        """
        if not self:
            return

        # Fetch all relevant mappings in one query
        mappings = self.env['ebay.product.mapping'].search([
            ('odoo_product_id', 'in', self.ids),
            ('sync_inventory', '=', True),
            ('active', '=', True),
            ('listing_status', 'in', ['active', 'out_of_stock']),
        ])
        if not mappings:
            return

        # Group by instance to batch efficiently
        instances = mappings.mapped('instance_id')
        now = fields.Datetime.now()

        for instance in instances:
            if instance.connection_status != 'connected':
                _logger.debug(
                    "eBay inventory push skipped: instance '%s' is not connected.",
                    instance.name,
                )
                continue

            instance_mappings = mappings.filtered(
                lambda m, inst=instance: m.instance_id == inst
            )

            try:
                # Delegate batching (≤ 25) to the instance method
                for i in range(0, len(instance_mappings), 25):
                    batch = instance_mappings[i:i + 25]
                    instance.sudo()._push_inventory_batch(
                        instance.sudo()._get_api_client(), batch, now
                    )
            except Exception:
                _logger.exception(
                    "eBay inventory push failed for instance '%s', "
                    "products: %s",
                    instance.name,
                    self.mapped('display_name'),
                )



# ---------------------------------------------------------------------------
# stock.quant — detect quantity changes and schedule inventory push
# ---------------------------------------------------------------------------

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def write(self, vals):
        """
        After writing quant records, schedule an eBay inventory push for any
        product whose available quantity may have changed in an internal location.

        We use env.cr.postcommit to defer the push until AFTER the current
        transaction commits, which means:
          • A transaction rollback (e.g. a constraint failure) will NOT trigger
            a push — eBay will never see quantities for a failed stock operation.
          • The API calls happen outside the SQL transaction, keeping the DB
            operation fast.
        """
        result = super().write(vals)

        if 'quantity' not in vals and 'reserved_quantity' not in vals:
            # Nothing stock-related changed; skip the overhead entirely.
            return result

        # Only internal storage locations affect sellable stock
        affected = self.filtered(
            lambda q: q.location_id.usage == 'internal'
        )
        if not affected:
            return result

        product_ids = affected.mapped('product_id').ids
        if not product_ids:
            return result

        # Capture IDs now; the recordset will be stale after commit
        env = self.env

        def _deferred_push():
            try:
                products = env['product.product'].sudo().browse(product_ids).exists()
                if products:
                    products._push_inventory_to_ebay()
            except Exception:
                _logger.exception(
                    "eBay deferred inventory push failed for product ids %s",
                    product_ids,
                )

        self.env.cr.postcommit.add(_deferred_push)
        return result
