import os, tempfile, threading, time, unittest
import sandbox_executor as se


class SandboxExecutorTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.sock = os.path.join(d, "exec.sock")
        threading.Thread(target=se.serve, args=(self.sock,), daemon=True).start()
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.02)

    def test_ping_reports_user(self):
        r = se.ping_sandbox(self.sock)
        self.assertTrue(r.get("ok"))
        self.assertTrue(r.get("user"))

    def test_terminal_stdout_and_exit(self):
        r = se.run_in_sandbox(self.sock, "terminal", "echo hello", timeout=10)
        self.assertEqual(r["stdout"].strip(), "hello")
        self.assertEqual(r["exit_code"], 0)
        self.assertFalse(r["timed_out"])

    def test_terminal_nonzero_exit_and_stderr(self):
        r = se.run_in_sandbox(self.sock, "terminal", "echo oops >&2; exit 3", timeout=10)
        self.assertIn("oops", r["stderr"])
        self.assertEqual(r["exit_code"], 3)

    def test_execute_code(self):
        r = se.run_in_sandbox(self.sock, "execute_code", "print(2 + 2)", timeout=10)
        self.assertEqual(r["stdout"].strip(), "4")
        self.assertEqual(r["exit_code"], 0)

    def test_timeout_is_reported_not_hung(self):
        r = se.run_in_sandbox(self.sock, "terminal", "sleep 5", timeout=1)
        self.assertTrue(r["timed_out"])
        self.assertEqual(r["exit_code"], 124)

    def test_large_output_framing(self):
        r = se.run_in_sandbox(self.sock, "execute_code", "print('x' * 200000)", timeout=10)
        self.assertEqual(len(r["stdout"].strip()), 200000)


if __name__ == "__main__":
    unittest.main()
