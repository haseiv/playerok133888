"""Run handlers in isolation: importing bot.py requires production credentials."""
import ast
from pathlib import Path
import re
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
nodes = [node for node in source.body if
         isinstance(node, ast.FunctionDef) and node.name == "_wants_code" or
         isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CODE_WORDS" for t in node.targets)]
namespace = {"re": re}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
wants_code = namespace["_wants_code"]


class CodeRequestTests(unittest.TestCase):
    def test_requests(self):
        for text in ("код", "КОД!", "Дайте коду", "кодик", "стимгард", "steam guard", "code",
                     "Здравствуйте! Я купил доступ и сейчас пытаюсь войти в аккаунт Steam. Пришлите код, пожалуйста."):
            with self.subTest(text=text):
                self.assertTrue(wants_code(text))

    def test_unrelated_words(self):
        for text in ("", "кодировка", "штрихкод", "decode", "кодекс", "привет"):
            with self.subTest(text=text):
                self.assertFalse(wants_code(text))


if __name__ == "__main__":
    unittest.main()
