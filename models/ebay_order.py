# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class EbayOrder(models.Model):
    _name = 'ebay.order'
    _description = 'eBay Order'
    _rec_name = 'ebay_order_id'
    _order = 'create_date desc'

    ebay_order_id = fields.Char(
        string='eBay Order ID',
        required=True,
        index=True,
        copy=False,
        help='The unique order identifier assigned by eBay (e.g. 12-12345-67890).',
    )
    instance_id = fields.Many2one(
        comodel_name='ebay.instance',
        string='eBay Instance',
        required=True,
        ondelete='restrict',
        index=True,
        help='The eBay account/instance this order was imported from.',
    )
    odoo_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Odoo Sale Order',
        ondelete='set null',
        index=True,
        copy=False,
        help='The corresponding Odoo Sales Order created from this eBay order.',
    )
    order_status = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('awaiting_payment', 'Awaiting Payment'),
            ('paid', 'Paid'),
            ('in_checkout', 'In Checkout'),
            ('cancelled', 'Cancelled'),
            ('shipped', 'Shipped'),
            ('completed', 'Completed'),
        ],
        string='eBay Order Status',
        required=True,
        default='pending',
        help='The current status of the order as reported by eBay.',
    )
    sync_status = fields.Selection(
        selection=[
            ('pending', 'Pending Sync'),
            ('synced', 'Synced'),
            ('error', 'Sync Error'),
            ('cancelled', 'Cancelled'),
        ],
        string='Sync Status',
        required=True,
        default='pending',
        index=True,
        help='Indicates whether this order has been successfully processed in Odoo.',
    )
    total_amount = fields.Monetary(
        string='Total Amount',
        currency_field='currency_id',
        help='The grand total of the eBay order including taxes and shipping.',
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    buyer_username = fields.Char(
        string='Buyer Username',
        help='The eBay username of the buyer.',
    )
    buyer_email = fields.Char(
        string='Buyer Email',
        help='The email address of the buyer provided by eBay.',
    )
    order_date = fields.Datetime(
        string='eBay Order Date',
        help='The date and time the order was created on eBay.',
    )
    last_sync_date = fields.Datetime(
        string='Last Sync Date',
        readonly=True,
        help='The last time this record was synchronised with eBay.',
    )
    sync_error_message = fields.Text(
        string='Sync Error Message',
        readonly=True,
        help='Details of the last synchronisation error, if any.',
    )
    fulfillment_status = fields.Selection(
        selection=[
            ('not_started', 'Not Started'),
            ('in_progress', 'In Progress'),
            ('fulfilled', 'Fulfilled'),
        ],
        string='Fulfillment Status',
        default='not_started',
        help='Tracks whether the shipping fulfillment has been pushed back to eBay.',
    )

    # ------------------------------------------------------------------ #
    #  SQL constraint: each eBay order ID must be unique per instance     #
    # ------------------------------------------------------------------ #

    _sql_constraints = [
        (
            'unique_ebay_order_per_instance',
            'UNIQUE(ebay_order_id, instance_id)',
            'An eBay order with this Order ID already exists for the selected instance.',
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Compute methods                                                     #
    # ------------------------------------------------------------------ #

    @api.depends('odoo_order_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.ebay_order_id or _('New')

    # ------------------------------------------------------------------ #
    #  Overrides                                                           #
    # ------------------------------------------------------------------ #

    def name_get(self):
        result = []
        for rec in self:
            name = rec.ebay_order_id
            if rec.odoo_order_id:
                name = '%s (%s)' % (name, rec.odoo_order_id.name)
            result.append((rec.id, name))
        return result
