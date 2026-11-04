import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


def handlers():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ('redeem_by_text', 'start_with_token', 'refresh_code'):
            node.decorator_list = []
            nodes.append(node)
    env = dict(Message=object, CommandObject=object, CallbackQuery=object,
               storage=SimpleNamespace(deal_by_token=AsyncMock()),
               MaFile=SimpleNamespace(parse=lambda _: SimpleNamespace(code=lambda: 'GUARD', seconds_left=lambda: 20)),
               _tg_text=lambda *a: 'credentials', _code_kb=lambda *a: None,
               _send_mafile=AsyncMock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'handlers', 'exec'), env)
    return env


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_rules(self):
        for active, expired, kind in ((False, False, 'rent'), (True, True, 'rent'),
                                      (False, False, 'sale'), (True, False, 'rent'),
                                      (True, False, 'shared'), (True, False, 'sale')):
            for name in ('redeem_by_text', 'start_with_token', 'refresh_code'):
                with self.subTest(active=active, expired=expired, kind=kind, handler=name):
                    env = handlers()
                    deal = SimpleNamespace(active=active, expired=lambda: expired,
                                           kind=kind, token='token', is_rent=kind == 'rent')
                    env['storage'].deal_by_token.return_value = (SimpleNamespace(mafile='{}'), deal)
                    msg = SimpleNamespace(text='token', data='code:token', answer=AsyncMock())
                    args = (msg, SimpleNamespace(args='token')) if name == 'start_with_token' else (msg,)
                    await env[name](*args)
                    if not active or expired:
                        self.assertNotIn('credentials', str(msg.answer.call_args))
                        self.assertNotIn('GUARD', str(msg.answer.call_args))
                    expected_file = active and not expired and kind == 'sale' and name != 'refresh_code'
                    self.assertEqual(env['_send_mafile'].await_count, int(expected_file))
