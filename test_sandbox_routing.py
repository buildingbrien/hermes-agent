import os, tempfile, threading, time, unittest
import sandbox_executor as se
import sandbox_routing as sr


class RoutingTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(sr.AGENT_SANDBOX_FLAG, None)

    def test_disabled_returns_none(self):
        os.environ.pop(sr.AGENT_SANDBOX_FLAG, None)
        self.assertIsNone(sr.maybe_run("terminal", "echo hi"))

    def test_enabled_but_no_socket_returns_none(self):
        os.environ[sr.AGENT_SANDBOX_FLAG] = "1"
        sr.SANDBOX_HOME_ROOT = tempfile.mkdtemp()      # nothing listening here
        self.assertIsNone(sr.maybe_run("terminal", "echo hi"))

    def test_routes_to_live_executor(self):
        os.environ[sr.AGENT_SANDBOX_FLAG] = "1"
        root = tempfile.mkdtemp()
        sr.SANDBOX_HOME_ROOT = root
        sock = sr.socket_path()                        # <root>/<agent>/executor.sock
        os.makedirs(os.path.dirname(sock), exist_ok=True)
        threading.Thread(target=se.serve, args=(sock,), daemon=True).start()
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.02)
        r = sr.maybe_run("terminal", "echo routed", timeout=10)
        self.assertIsNotNone(r)
        self.assertEqual(r["stdout"].strip(), "routed")
        self.assertEqual(r["exit_code"], 0)

    def test_agent_id_from_hermes_home(self):
        os.environ["HERMES_HOME"] = "/Users/x/.hermes/profiles/neith"
        self.assertEqual(sr.agent_id(), "neith")
        os.environ.pop("HERMES_HOME", None)
