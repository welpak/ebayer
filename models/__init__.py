# -*- coding: utf-8 -*-

from . import ebay_instance
from . import ebay_order
from . import ebay_product
from . import res_config_settings
from . import ebay_api_client   # extends ebay.instance; must follow ebay_instance
from . import sale_order        # extends sale.order + ebay.order + sale.order.line
from . import stock_picking     # extends stock.picking
from . import product           # extends product.template, product.product, stock.quant
