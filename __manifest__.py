# -*- coding: utf-8 -*-
{
    'name': 'eBay Connector',
    'version': '16.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Bi-directional integration between Odoo and eBay via REST APIs',
    'description': """
eBay Connector
==============
A production-ready module providing bi-directional integration between Odoo 16
and eBay using the modern eBay REST APIs.

Features:
---------
* Multi-instance eBay account support (Sandbox & Production)
* Automatic order import from eBay to Odoo Sale Orders
* Bi-directional inventory synchronisation
* Shipping fulfillment updates pushed back to eBay
* Webhook (Instant Sync) and Cron (Batch Sync) support
    """,
    'author': 'eBay Connector Team',
    'website': '',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'sale_management',
        'stock',
        'delivery',
    ],
    'data': [
        'security/ir.model.access.csv',
    ],
    'demo': [],
    'installable': True,
    'application': True,
    'auto_install': False,
}
