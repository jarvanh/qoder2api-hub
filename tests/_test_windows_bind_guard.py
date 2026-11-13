"""Windows listener exclusion: atomic kernel bind, no probe/reuse fallback."""
import os
import socket
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish
P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])
with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "qoder_proxy.py"), encoding="utf-8") as fh:
    src = fh.read()
check("reuse policy matches platform", P._ExclusiveHTTPServer.allow_reuse_address == (not P.IS_WINDOWS))
check("normal startup uses exclusive server", "server = _ExclusiveHTTPServer((args.host, args.port), Handler)" in src)
check("no unsafe compatibility server fallback", "_CompatHTTPServer" not in src)

def rejected(server):
    try:
        extra = P._ExclusiveHTTPServer(("127.0.0.1", server.getsockname()[1]), P.Handler)
    except OSError:
        return True
    else:
        extra.server_close()
        return False

for reuse in (0, 1):
    for listening in (False, True):
        owner = socket.socket()
        owner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, reuse)
        owner.bind(("127.0.0.1", 0))
        if listening:
            owner.listen(1)
        try:
            if P.IS_WINDOWS or listening:
                check("reject occupied socket reuse=%s listening=%s" % (reuse, listening), rejected(owner))
            else:
                # POSIX reusable sockets may share a bound-but-not-listening
                # address. Listener exclusion, not pre-listen reservation, is
                # the cross-platform contract.
                check("POSIX owner remains bound before listen reuse=%s" % reuse, owner.getsockname()[1] > 0)
        finally:
            owner.close()

server = P._ExclusiveHTTPServer(("127.0.0.1", 0), P.Handler)
port = server.server_port
client = socket.create_connection(("127.0.0.1", port), timeout=2)
accepted, _ = server.socket.accept()
client.close()
accepted.settimeout(2)
check("server is actually listening", accepted.recv(1) == b"")
accepted.close()
if P.IS_WINDOWS:
    check("kernel exclusive option enabled", server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1)
    intruder = socket.socket()
    intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            intruder.bind(("127.0.0.1", port))
            intruder.listen(1)
        except OSError:
            blocked = True
        else:
            blocked = False
        check("exclusive listener cannot be stolen by reusable socket", blocked)
    finally:
        intruder.close()
else:
    check("POSIX keeps TIME_WAIT reuse enabled", server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1)
server.server_close()
reopened = P._ExclusiveHTTPServer(("127.0.0.1", port), P.Handler)
check("closed listener can reopen without fallback", reopened.server_port == port)
reopened.server_close()
finish()
