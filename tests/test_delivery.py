import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
import crypto
from config import cfg
from cryptography.fernet import Fernet
from db import Storage
from playerok import Order


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Storage(str(Path(self.tmp.name) / 'store.db'))
        cfg.fernet_key = Fernet.generate_key().decode()
        crypto._fernet = None
        await self.store.connect()
        self.market = AsyncMock()
        self.market.send_message.return_value = True
        self.patches = [patch.object(bot, 'storage', self.store),
                        patch.object(bot, 'market', self.market),
                        patch.object(bot, '_order_lock', asyncio.Lock()),
                        patch.object(bot, 'notify_admins', AsyncMock()),
                        patch.object(bot, '_after_sale', AsyncMock()),
                        patch.object(cfg, 'auto_confirm', False)]
        for item in self.patches:
            item.start()
        await self.store.link_lot('item', 'product')
        self.order = Order('order', 'item', 'title', 'chat', 'buyer')

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.store.close()
        self.tmp.cleanup()

    async def reopen(self):
        await self.store.close()
        await self.store.connect()

    async def test_steam_retry_survives_restart_and_reuses_account(self):
        await self.store.add_account('product', 'login', 'password', '{"shared_secret":"dGVzdA=="}')
        self.market.send_message.return_value = False
        await bot.handle_order(self.order)
        self.assertFalse(await self.store.delivery_complete('order'))
        pending = await self.store.deal_by_order('order')
        await self.reopen()
        self.assertFalse(await self.store.delivery_complete('order'))
        self.market.send_message.return_value = True
        await bot.handle_order(self.order)
        self.assertTrue(await self.store.delivery_complete('order'))
        self.assertEqual((await self.store.deal_by_order('order'))[0].id, pending[0].id)
        await bot.handle_order(self.order)
        self.assertEqual(self.market.send_message.await_count, 2)
        self.assertEqual(await self.store.failed_delivery_count(), 0)

    async def test_shared_retry(self):
        account_id = await self.store.add_account('product', 'login', 'pw', '{"shared_secret":"dGVzdA=="}')
        await self.store.set_status(account_id, 'shared')
        self.market.send_message.return_value = False
        await bot.handle_order(self.order)
        self.market.send_message.return_value = True
        await bot.handle_order(self.order)
        self.assertTrue(await self.store.delivery_complete('order'))
        self.assertEqual((await self.store.account_by_id(account_id)).rents_count, 1)

    async def test_code_retry_and_concurrent_duplicate(self):
        await self.store.make_codes_product('product')
        await self.store.add_codes('product', ['KEY1', 'KEY2'])
        self.market.send_message.return_value = False
        await bot.handle_order(self.order)
        await self.reopen()
        self.market.send_message.return_value = True
        await asyncio.gather(bot.handle_order(self.order), bot.handle_order(self.order))
        self.assertEqual(await self.store.codes_left('product'), 1)
        self.assertEqual(self.market.send_message.await_count, 2)
        for call in self.market.send_message.call_args_list:
            self.assertIn('KEY1', call.args[1])
        self.assertEqual((await self.store.get_digital('product'))['sold_count'], 1)

    async def test_file_links_survive_restart(self):
        await self.store.set_digital_file('product', 'file-id', 'file.zip')
        await bot.handle_order(self.order)
        text = self.market.send_message.call_args.args[1]
        token = text.split('file_')[1]
        await self.reopen()
        self.assertEqual((await self.store.file_by_token(token))['file_id'], 'file-id')
        self.assertEqual(await self.store.file_token('order', {}), token)

    async def test_legacy_success_and_failure_migration(self):
        await self.store.add_account('product', 'login', 'pw', '{}')
        await self.store.take_account('product', order_id='legacy-success')
        await self.store.add_account('product', 'login2', 'pw', '{}')
        await self.store.take_account('product', order_id='legacy-failure')
        await self.store.finish_deal('legacy-failure', 'free')
        await self.store.queue_failed_delivery('legacy-failure', 'item', 'title', 'chat', 'buyer')
        await self.reopen()
        self.assertTrue(await self.store.delivery_complete('legacy-success'))
        self.assertFalse(await self.store.delivery_complete('legacy-failure'))
        self.assertFalse(await self.store.deal_exists('legacy-failure'))
