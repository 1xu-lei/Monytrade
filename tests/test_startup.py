"""app.py 启动期：端口被占时要给可照做的提示，而不是甩一屏堆栈。

复现的是真实场景：另一个项目用 uvicorn 占了 8000（asyncio 会设
SO_EXCLUSIVEADDRUSE），Wallet 再绑同一端口，Windows 报的是
WinError 10013（访问权限不允许），而不是常见的 10048（端口已占用）。
"""

import contextlib
import io
import socket
import unittest

import app


class FreePortTest(unittest.TestCase):

    def test_returns_bindable_port_above_the_busy_one(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", 0))
            port = busy.getsockname()[1]

            suggestion = app._free_port("127.0.0.1", port)

        self.assertIsNotNone(suggestion)
        self.assertGreater(suggestion, port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", suggestion))  # 建议的端口必须真的能绑上


@unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"),
                     "SO_EXCLUSIVEADDRUSE 只在 Windows 上有")
class PortConflictTest(unittest.TestCase):
    """锁住「端口被独占占用」这条路径：winerror 10013 也要翻译成人话。"""

    def occupy(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    def test_occupied_port_exits_with_actionable_hint(self):
        port = self.occupy()
        output = io.StringIO()

        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                app._make_server("127.0.0.1", port)

        text = output.getvalue()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn(str(port), text)                  # 说清是哪个端口
        self.assertIn("--port", text)                   # 给出可直接照做的命令
        self.assertIn("Get-NetTCPConnection", text)     # 给出查占用者的方法
        self.assertNotIn("Traceback", text)             # 不甩堆栈


if __name__ == "__main__":
    unittest.main()
