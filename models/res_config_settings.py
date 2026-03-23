# -*- coding: utf-8 -*-

from odoo import api, fields, models, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ------------------------------------------------------------------
    # Primary instance selector
    # ------------------------------------------------------------------
    ebay_instance_id = fields.Many2one(
        comodel_name='ebay.instance',
        string='Primary eBay Instance',
        help='The eBay account displayed and configured via this Settings panel. '
             'Per-instance credentials and sync toggles are saved directly to '
             'the selected instance record.',
    )

    # ------------------------------------------------------------------
    # Credentials — read from / written back to the selected instance
    # ------------------------------------------------------------------
    ebay_environment = fields.Selection(
        selection=[
            ('sandbox', 'Sandbox'),
            ('production', 'Production'),
        ],
        string='Environment',
        default='sandbox',
    )
    ebay_app_id = fields.Char(string='App ID (Client ID)')
    ebay_dev_id = fields.Char(string='Dev ID')
    ebay_cert_id = fields.Char(string='Cert ID (Client Secret)')
    ebay_connection_status = fields.Selection(
        selection=[
            ('not_connected', 'Not Connected'),
            ('connected', 'Connected'),
            ('error', 'Connection Error'),
        ],
        string='Connection Status',
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Sync toggles — stored on ebay.instance
    # ------------------------------------------------------------------
    ebay_instant_sync = fields.Boolean(
        string='Enable Instant Sync (Webhooks)',
        help='Receive eBay notifications (order updates, policy changes) '
             'in real time via webhooks.',
    )
    ebay_batch_sync = fields.Boolean(
        string='Enable Batch Sync (Scheduled Jobs)',
        help='Synchronise orders and inventory on a recurring schedule '
             'via Odoo\'s scheduler (ir.cron). Enabling this activates '
             'the eBay cron jobs; disabling it deactivates them.',
    )

    # ------------------------------------------------------------------
    # Cron / scheduler interval (applied to both eBay ir.cron records)
    # ------------------------------------------------------------------
    ebay_cron_interval_number = fields.Integer(
        string='Sync Every',
        default=30,
        help='Numeric part of the sync interval (e.g. 30 for "every 30 minutes").',
    )
    ebay_cron_interval_type = fields.Selection(
        selection=[
            ('minutes', 'Minutes'),
            ('hours', 'Hours'),
            ('days', 'Days'),
            ('weeks', 'Weeks'),
        ],
        string='Interval Unit',
        default='minutes',
    )

    # ------------------------------------------------------------------
    # get_values — populate the form from persistent storage
    # ------------------------------------------------------------------
    @api.model
    def get_values(self):
        res = super().get_values()

        ICP = self.env['ir.config_parameter'].sudo()

        # Resolve the stored primary instance
        raw = ICP.get_param('ebay_connector.primary_instance_id', default=False)
        instance_id = int(raw) if raw and str(raw).isdigit() else 0
        instance = (
            self.env['ebay.instance'].browse(instance_id).exists()
            if instance_id else self.env['ebay.instance']
        )

        res['ebay_instance_id'] = instance.id if instance else False

        if instance:
            res.update({
                'ebay_environment':      instance.environment,
                'ebay_app_id':           instance.app_id,
                'ebay_dev_id':           instance.dev_id,
                'ebay_cert_id':          instance.cert_id,
                'ebay_connection_status': instance.connection_status,
                'ebay_instant_sync':     instance.enable_instant_sync,
                'ebay_batch_sync':       instance.enable_batch_sync,
            })
        else:
            res.update({
                'ebay_environment':      'sandbox',
                'ebay_app_id':           False,
                'ebay_dev_id':           False,
                'ebay_cert_id':          False,
                'ebay_connection_status': 'not_connected',
                'ebay_instant_sync':     False,
                'ebay_batch_sync':       False,
            })

        # Pull cron interval from the fetch-orders cron record
        # (both eBay crons share the same interval setting)
        fetch_cron = self.env.ref(
            'ebay_connector.ir_cron_fetch_orders', raise_if_not_found=False
        )
        if fetch_cron:
            res['ebay_cron_interval_number'] = fetch_cron.interval_number
            res['ebay_cron_interval_type']   = fetch_cron.interval_type
        else:
            res['ebay_cron_interval_number'] = 30
            res['ebay_cron_interval_type']   = 'minutes'

        return res

    # ------------------------------------------------------------------
    # set_values — persist all eBay settings when the user clicks Save
    # ------------------------------------------------------------------
    def set_values(self):
        super().set_values()

        ICP = self.env['ir.config_parameter'].sudo()

        # 1. Persist the selected primary instance ID
        ICP.set_param(
            'ebay_connector.primary_instance_id',
            self.ebay_instance_id.id or 0,
        )

        # 2. Write credentials + sync flags back to the instance record
        if self.ebay_instance_id:
            self.ebay_instance_id.sudo().write({
                'environment':        self.ebay_environment or 'sandbox',
                'app_id':             self.ebay_app_id or '',
                'dev_id':             self.ebay_dev_id or '',
                'cert_id':            self.ebay_cert_id or '',
                'enable_instant_sync': bool(self.ebay_instant_sync),
                'enable_batch_sync':   bool(self.ebay_batch_sync),
            })

        # 3. Update both eBay ir.cron records:
        #    - active  → mirrors the batch sync toggle
        #    - interval → taken from the interval fields
        cron_vals = {
            'active':          bool(self.ebay_batch_sync),
            'interval_number': max(1, self.ebay_cron_interval_number or 30),
            'interval_type':   self.ebay_cron_interval_type or 'minutes',
        }
        for xmlid in (
            'ebay_connector.ir_cron_fetch_orders',
            'ebay_connector.ir_cron_sync_inventory',
        ):
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron:
                cron.sudo().write(cron_vals)

    # ------------------------------------------------------------------
    # onchange — refresh credential panel when the instance is swapped
    # ------------------------------------------------------------------
    @api.onchange('ebay_instance_id')
    def _onchange_ebay_instance_id(self):
        instance = self.ebay_instance_id
        if instance:
            self.ebay_environment      = instance.environment
            self.ebay_app_id           = instance.app_id
            self.ebay_dev_id           = instance.dev_id
            self.ebay_cert_id          = instance.cert_id
            self.ebay_connection_status = instance.connection_status
            self.ebay_instant_sync     = instance.enable_instant_sync
            self.ebay_batch_sync       = instance.enable_batch_sync
        else:
            self.ebay_environment      = 'sandbox'
            self.ebay_app_id           = False
            self.ebay_dev_id           = False
            self.ebay_cert_id          = False
            self.ebay_connection_status = 'not_connected'
            self.ebay_instant_sync     = False
            self.ebay_batch_sync       = False
