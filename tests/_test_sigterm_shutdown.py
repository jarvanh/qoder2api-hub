"""Issue #156: TERM must unwind PID1's server loop, close listener and run atexit."""
import atexit
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import ROOT, bootstrap, check, skip, declare_suite, finish
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", ".github/workflows/docker-publish.yml"])

class Server:
    def __init__(self, action):
        self.action = action
        self.closed = False
    def serve_forever(self):
        self.action()
    def server_close(self):
        self.closed = True
    def shutdown(self):
        raise AssertionError("signal handler must not wait on the serving thread")

original = signal.getsignal(signal.SIGTERM)
server = Server(lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))
try:
    P.serve_http_server(server)
except SystemExit as exc:
    check("TERM exits successfully", exc.code == 0)
else:
    check("TERM exits successfully", False)
check("TERM closes server", server.closed)
check("TERM restores caller signal handler", signal.getsignal(signal.SIGTERM) == original)
class RepeatedTermServer(Server):
    def server_close(self):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.closed = True
server = RepeatedTermServer(lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))
try:
    P.serve_http_server(server)
except SystemExit:
    pass
check("repeated TERM does not interrupt close", server.closed and signal.getsignal(signal.SIGTERM) == original)
server = Server(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
with mock.patch.object(P, "log") as log:
    P.serve_http_server(server)
    check("Ctrl-C keeps normal shutdown", server.closed and log.called)
server = Server(lambda: (_ for _ in ()).throw(RuntimeError("fixture")))
try:
    P.serve_http_server(server)
except RuntimeError:
    check("unexpected failure still closes and restores", server.closed and signal.getsignal(signal.SIGTERM) == original)
else:
    check("unexpected failure is not swallowed", False)
server = Server(lambda: None)
worker_errors = []
def run_worker():
    try:
        P.serve_http_server(server)
    except BaseException as exc:
        worker_errors.append(repr(exc))
t = threading.Thread(target=run_worker)
t.start(); t.join(3)
check("non-main serving does not register signals", not t.is_alive() and not worker_errors and server.closed)

if os.name != "nt":
    # Real process signal, real listening socket, no network/API/account material.
    program = r'''
import atexit, os, socket, sys
sys.path.insert(0, sys.argv[1])
import qoder_proxy as P
from http.server import BaseHTTPRequestHandler
from pathlib import Path
root = Path(sys.argv[2])
(root / "fixture.pid").write_text(str(os.getpid()))
def cleaned():
    (root / "fixture.pid").unlink()
    (root / "atexit").write_text("done")
atexit.register(cleaned)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
server = P._ExclusiveHTTPServer(("127.0.0.1", 0), Handler)
serve = server.serve_forever
def ready():
    (root / "ready").write_text(str(server.server_port))
    serve(poll_interval=0.05)
server.serve_forever = ready
P.serve_http_server(server)
'''
    for i in range(2):
        with tempfile.TemporaryDirectory(prefix="qd-term-") as tmp:
            env = dict(os.environ, ACCOUNTS_DIR=os.path.join(tmp, "accounts"), QD_PROXY_USAGE_DIR=os.path.join(tmp, "usage"))
            proc = subprocess.Popen([sys.executable, "-u", "-c", program, ROOT, tmp], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not os.path.exists(os.path.join(tmp, "ready")) and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                ready = os.path.exists(os.path.join(tmp, "ready"))
                check("real server ready %s" % i, ready)
                if ready:
                    started = time.monotonic()
                    proc.send_signal(signal.SIGTERM)
                    out, err = proc.communicate(timeout=3)
                    check("real TERM exits without deadlock %s" % i, proc.returncode == 0 and time.monotonic() - started < 3, err.decode()[-500:])
                    check("real TERM runs atexit and pid cleanup %s" % i,
                          os.path.exists(os.path.join(tmp, "atexit")) and not os.path.exists(os.path.join(tmp, "fixture.pid")))
            finally:
                if proc.poll() is None:
                    proc.kill();proc.communicate(timeout=3)
else:
    skip("POSIX real SIGTERM subprocess", "Windows terminate is not a POSIX signal; handler path tested directly")
finish()
