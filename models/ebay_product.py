# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class EbayProductMapping(models.Model):
    _name = 'ebay.product.mapping'
    _description = 'eBay Product Mapping'
    _rec_name = 'ebay_item_id'
    _order = 'write_date desc'

    ebay_item_id = fields.Char(
        string='eBay Item ID',
        required=True,
        index=True,
        copy=False,
        help='The unique listing/item identifier assigned by eBay.',
    )
    instance_id = fields.Many2one(
        comodel_name='ebay.instance',
        string='eBay Instance',
        required=True,
        ondelete='restrict',
        index=True,
        help='The eBay account/instance this listing belongs to.',
    )
    odoo_product_id = fields.Many2one(
        comodel_name='product.product',
        string='Odoo Product Variant',
        required=True,
        ondelete='restrict',
        index=True,
        help='The Odoo product variant linked to this eBay listing.',
    )
    odoo_product_tmpl_id = fields.Many2one(
        comodel_name='product.template',
        string='Odoo Product Template',
        related='odoo_product_id.product_tmpl_id',
        store=True,
        readonly=True,
    )
    ebay_sku = fields.Char(
        string='eBay SKU',
        index=True,
        help='The seller-defined SKU on the eBay listing, used to match eBay '
             'order lines back to Odoo products.',
    )
    sync_inventory = fields.Boolean(
        string='Sync Inventory',
        default=True,
        help='When enabled, Odoo stock quantity changes for this product are '
             'automatically pushed to the corresponding eBay listing.',
    )
    ebay_quantity = fields.Integer(
        string='eBay Listed Quantity',
        default=0,
        help='The quantity currently listed on eBay for this item.',
    )
    last_inventory_sync = fields.Datetime(
        string='Last Inventory Sync',
        readonly=True,
        help='The last time inventory was successfully synchronised to eBay.',
    )
    listing_status = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('ended', 'Ended'),
            ('out_of_stock', 'Out of Stock'),
            ('unknown', 'Unknown'),
        ],
        string='Listing Status',
        default='unknown',
        help='The current status of the eBay listing.',
    )
    sync_error_message = fields.Text(
        string='Sync Error Message',
        readonly=True,
        help='Details of the last inventory synchronisation error, if any.',
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Deactivate to stop inventory sync without deleting the mapping record.',
    )

    # ------------------------------------------------------------------ #
    #  SQL constraint: each eBay item ID must be unique per instance      #
    # ------------------------------------------------------------------ #

    _sql_constraints = [
        (
            'unique_ebay_item_per_instance',
            'UNIQUE(ebay_item_id, instance_id)',
            'A mapping for this eBay Item ID already exists for the selected instance.',
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Overrides                                                           #
    # ------------------------------------------------------------------ #

    def name_get(self):
        result = []
        for rec in self:
            product_name = rec.odoo_product_id.display_name if rec.odoo_product_id else _('N/A')
            name = '[%s] %s' % (rec.ebay_item_id, product_name)
            result.append((rec.id, name))
        return result

    # ------------------------------------------------------------------ #
    #  Constraints                                                         #
    # ------------------------------------------------------------------ #

    @api.constrains('odoo_product_id', 'instance_id')
    def _check_unique_product_per_instance(self):
        """Prevent the same Odoo product variant from being mapped twice
        to the same eBay instance."""
        for rec in self:
            domain = [
                ('id', '!=', rec.id),
                ('odoo_product_id', '=', rec.odoo_product_id.id),
                ('instance_id', '=', rec.instance_id.id),
            ]
            if self.search_count(domain):
                raise ValidationError(
                    _('Product "%s" is already mapped to an eBay listing '
                      'on instance "%s".')
                    % (rec.odoo_product_id.display_name, rec.instance_id.name)
                )
