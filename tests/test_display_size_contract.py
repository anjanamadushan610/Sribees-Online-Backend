"""Dependency-free checks for customer display metadata."""
import ast
import asyncio
import sys
import types
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch
from uuid import UUID
ROOT = Path(__file__).resolve().parents[1]
class DisplaySizeContract(unittest.TestCase):
    def test_checkout_uses_catalogue_display_fields_without_changing_weight(self):
        pid = '11111111-1111-4111-8111-111111111111'
        class Products:
            @staticmethod
            async def get_price_map(db, ids, branch): return {pid: 250}
            @staticmethod
            async def get_display_size_map(db, ids): return {pid: ('400', 'g')}
        class Options:
            @staticmethod
            async def price_map(db, ids, branch): return {}
        module = types.ModuleType('app.services.product_service')
        module.ProductService = Products
        tree = ast.parse((ROOT / 'app/services/order_service.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_attach_server_prices')
        method.decorator_list = []
        scope = {'AsyncSession': object, 'List': List, 'Optional': Optional, 'UUID': UUID, 'weight_options_service': Options}
        with patch.dict(sys.modules, {'app': types.ModuleType('app'), 'app.services': types.ModuleType('app.services'), 'app.services.product_service': module}):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<source>', 'exec'), scope)
            result = asyncio.run(scope['_attach_server_prices'](None, [{'product_id': pid, 'price': 1, 'display_size': 'fake', 'weight_kg': .430}], None))
        self.assertEqual(result[0]['price'], 250)
        self.assertEqual(result[0]['display_size'], '400')
        self.assertEqual(result[0]['display_size_unit'], 'g')
        self.assertEqual(result[0]['weight_kg'], .430)
    def test_migration_does_not_backfill_delivery_weight(self):
        sql = (ROOT / 'migrations/093_product_display_size.sql').read_text().upper()
        self.assertNotIn('UPDATE ', sql)
        self.assertNotIn('DROP ', sql)
        self.assertIn('DISPLAY_SIZE_UNIT', sql)
