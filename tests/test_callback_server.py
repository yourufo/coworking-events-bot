import unittest
from server import build_callback_page


class CallbackServerTests(unittest.TestCase):
    def test_build_callback_page_includes_params(self):
        html = build_callback_page({"code": "demo-code", "state": "demo-state"})

        self.assertIn("demo-code", html)
        self.assertIn("demo-state", html)
        self.assertIn("callback", html.lower())


if __name__ == "__main__":
    unittest.main()
