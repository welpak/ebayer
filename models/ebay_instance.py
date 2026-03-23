# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class EbayInstance(models.Model):
    _name = 'ebay.instance'
    _description = 'eBay Instance'
    _rec_name = 'name'

    name = fields.Char(
        string='Instance Name',
        required=True,
        help='A descriptive label to identify this eBay account/instance.',
    )
    environment = fields.Selection(
        selection=[
            ('sandbox', 'Sandbox'),
            ('production', 'Production'),
        ],
        string='Environment',
        required=True,
        default='sandbox',
        help='Select Sandbox for testing or Production for live eBay transactions.',
    )
    app_id = fields.Char(
        string='App ID (Client ID)',
        required=True,
        help='The Client ID issued by eBay Developer Program for your application.',
    )
    dev_id = fields.Char(
        string='Dev ID',
        required=True,
        help='The Developer ID issued by eBay Developer Program.',
    )
    cert_id = fields.Char(
        string='Cert ID (Client Secret)',
        required=True,
        help='The Client Secret (Cert ID) issued by eBay Developer Program.',
    )
    access_token = fields.Text(
        string='Access Token',
        help='The OAuth 2.0 access token used for authenticating eBay REST API calls.',
    )
    refresh_token = fields.Text(
        string='Refresh Token',
        help='The OAuth 2.0 refresh token used to obtain a new access token when it expires.',
    )
    token_expires_at = fields.Datetime(
        string='Token Expires At',
        help='The UTC datetime at which the current access token will expire.',
    )
    connection_status = fields.Selection(
        selection=[
            ('not_connected', 'Not Connected'),
            ('connected', 'Connected'),
            ('error', 'Connection Error'),
        ],
        string='Connection Status',
        default='not_connected',
        readonly=True,
        help='The current connection state between Odoo and the eBay API.',
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Deactivate an instance to prevent it from being used without deleting it.',
    )

    # Sync configuration toggles (used by Phase 2+ logic)
    enable_instant_sync = fields.Boolean(
        string='Enable Instant Sync (Webhooks)',
        default=False,
        help='When enabled, eBay marketplace account deletion and other notifications '
             'are received via webhooks for immediate processing.',
    )
    enable_batch_sync = fields.Boolean(
        string='Enable Batch Sync (Cron)',
        default=True,
        help='When enabled, orders and inventory are synchronised on a scheduled interval.',
    )

    # Computed / display helpers
    is_token_expired = fields.Boolean(
        string='Token Expired',
        compute='_compute_is_token_expired',
        store=False,
    )

    # ------------------------------------------------------------------ #
    #  Compute methods                                                     #
    # ------------------------------------------------------------------ #

    @api.depends('token_expires_at')
    def _compute_is_token_expired(self):
        now = fields.Datetime.now()
        for rec in self:
            rec.is_token_expired = bool(
                rec.token_expires_at and rec.token_expires_at <= now
            )

    # ------------------------------------------------------------------ #
    #  Constraints                                                         #
    # ------------------------------------------------------------------ #

    @api.constrains('app_id', 'environment')
    def _check_unique_app_per_environment(self):
        for rec in self:
            domain = [
                ('id', '!=', rec.id),
                ('app_id', '=', rec.app_id),
                ('environment', '=', rec.environment),
            ]
            if self.search_count(domain):
                raise ValidationError(
                    _('An eBay instance with App ID "%s" already exists for the %s environment.')
                    % (rec.app_id, rec.environment)
                )

    # ------------------------------------------------------------------ #
    #  Helper: resolve the correct REST API base URL for this instance    #
    # ------------------------------------------------------------------ #

    def _get_api_base_url(self):
        """Return the eBay REST API base URL for the current environment."""
        self.ensure_one()
        if self.environment == 'production':
            return 'https://api.ebay.com'
        return 'https://api.sandbox.ebay.com'
