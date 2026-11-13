import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_temp = tempfile.TemporaryDirectory()
os.environ["X_MCP_HOME"] = _temp.name
import server
import login

class PackageTests(unittest.TestCase):
    def test_cookies_only_import_x_domains(self):
        path = Path(_temp.name) / "cookies.json"
        path.write_text(json.dumps([
            {"name": "auth_token", "value": "fake", "domain": ".x.com", "expirationDate": 2000000000, "sameSite": "no_restriction"},
            {"name": "other", "value": "fake", "domain": "evil-x.com"},
        ]))
        cookies = login.load_cookies(path)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["sameSite"], "None")
        self.assertEqual(cookies[0]["expires"], 2000000000)

    def test_save_login_cookies_and_preserve_backup_if_not_signed_in(self):
        path = Path(_temp.name) / "saved.json"
        class Context:
            def __init__(self, cookies):
                self.items = cookies
            def cookies(self, urls):
                return self.items
        cookies = [{"name": "auth_token", "value": "fake", "domain": ".x.com", "path": "/"}]
        login.save_cookies(Context(cookies), path)
        self.assertEqual(login.load_cookies(path)[0]["value"], "fake")
        with self.assertRaises(RuntimeError):
            login.save_cookies(Context([]), path)
        self.assertEqual(login.load_cookies(path)[0]["value"], "fake")

    def test_reject_unrelated_cookie_export(self):
        path = Path(_temp.name) / "unrelated.json"
        path.write_text('[{"domain": "example.com"}]')
        with self.assertRaises(ValueError):
            login.load_cookies(path)

    def test_refuse_unapproved_write(self):
        with self.assertRaises(ValueError):
            server.gate(False)

    def test_refuse_external_post_url(self):
        with self.assertRaises(ValueError):
            server.get_tweet("https://evil.example/post")

    def test_uncertain_submission_never_retries(self):
        async def run():
            with patch.object(server, "tab_ctx") as tab:
                cm = tab.return_value
                cm.__aenter__ = AsyncMock(side_effect=RuntimeError("lost connection"))
                cm.__aexit__ = AsyncMock()
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    await server._write("https://x.com/compose/post", "button", "hello", None)
                self.assertEqual(cm.__aenter__.await_count, 1)
        asyncio.run(run())

    def test_stdio_server_lists_tools(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async def run():
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env={**os.environ, "X_MCP_HOME": _temp.name})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    names = {tool.name for tool in result.tools}
                    self.assertTrue({"read_search", "read_profile", "whoami", "create_post", "get_tweet"} <= names)
        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
