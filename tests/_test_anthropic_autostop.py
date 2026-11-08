# -*- coding: utf-8 -*-
"""真实 Claude 会话中的自动停止：丢失工具调用不能按成功结束。"""
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish

TMP = tempfile.TemporaryDirectory(prefix='qoder-autostop-test-')
os.environ['ACCOUNTS_DIR'] = os.path.join(TMP.name, 'accounts')
os.environ['QD_PROXY_USAGE_DIR'] = os.path.join(TMP.name, 'usage')
os.environ['QD_NATIVE_IDENTITY'] = '0'
for key in ('QD_STRUCTURED_TOOL_HISTORY', 'QD_STRUCTURED_TOOL_HISTORY_REALMS'):
    os.environ.pop(key, None)
P, A = bootstrap()
declare_suite(__file__, ['qoder_proxy.py', 'qoder_anthropic.py'])
P.ACCOUNTS_DIR = os.environ['ACCOUNTS_DIR']
P.CURRENT_REALM = 'intl'
P.API_KEY = 'test-autostop'
P.configured_keys = lambda: []
P.POOL = A.AccountPool(P.ACCOUNTS_DIR, log=lambda *a, **kw: None)
P.POOL.add(A.Account(dict(uid='fake-autostop', realm='intl', accessToken='fake-token')))

# 所有非环回网络都禁止，真实上游由 open_upstream 桩替代。
real_connect = socket.socket.connect
network_attempts = []
def local_only(sock, address):
    if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
        network_attempts.append(address)
        raise AssertionError('离线用例禁止连接上游')
    return real_connect(sock, address)

def no_network(*args, **kwargs):
    network_attempts.append('http_json')
    raise AssertionError('离线用例禁止调用真实上游')

ARGS = {'command': 'echo \'{"a":"b\\"c"}\'', 'description': '验证中文和转义'}
GOOD = P.LEAK_MARKER + '\n' + json.dumps([{'name': 'Bash', 'arguments': json.dumps(json.dumps(ARGS, ensure_ascii=False))}], ensure_ascii=False)
PARTIAL = P.LEAK_MARKER + '\n' + '[{"name":"Bash","arguments":"{\\"command\\":\\"echo '
NAMED = '好的，依次执行这两条命令。\n\n**第一条命令：**\n\n[Bash 工具调用]\n' + json.dumps(ARGS, ensure_ascii=False)
CURRENT = {'text': PARTIAL, 'size': 1, 'requests': []}

def frame(delta, fin=None):
    return {'id': 'autostop', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': fin}]}

def upstream(payload, **kwargs):
    CURRENT['requests'].append(payload)
    text, size = CURRENT['text'], CURRENT['size']
    chunks = [frame({'content': text[i:i+size]}) for i in range(0, len(text), size)]
    chunks.append(frame({}, 'stop'))
    chunks.append({'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 12}})
    rows = [b'data: ' + json.dumps({'statusCodeValue': 200, 'body': json.dumps(c, ensure_ascii=False)}, ensure_ascii=False).encode() + b'\n\n' for c in chunks]
    rows.append(b'data: {"body":"[DONE]"}\n\n')
    return io.BytesIO(b''.join(rows)), P.POOL.accounts[0], ''

class Handler(P.Handler):
    def log_message(self, *args): pass

def events(raw):
    return [json.loads(line[5:].strip()) for line in raw.decode().splitlines() if line.startswith('data:') and line[5:].strip() != '[DONE]']

with mock.patch.object(socket.socket, 'connect', local_only), mock.patch.object(A, 'http_json', no_network), mock.patch.object(P, 'open_upstream', upstream):
    server = P.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    def post(stream):
        payload = {'model': 'GLM-5.3', 'max_tokens': 1024, 'stream': stream, 'messages': [{'role': 'user', 'content': '继续执行下一条命令'}], 'tools': [{'name': 'Bash', 'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}]}
        conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
        conn.request('POST', '/v1/messages', json.dumps(payload), {'x-api-key': 'test-autostop', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        status, raw = response.status, response.read()
        conn.close()
        return status, raw
    try:
        for text, label in ((PARTIAL, '截断数组'), (NAMED, '伪 Bash 调用')):
            for size in (1, 7, len(text)):
                CURRENT.update(text=text, size=size)
                status, raw = post(True)
                evs = events(raw)
                # fork 惰性建流：首帧前失败直接回真实 HTTP 502（客户端可归类轮换），
                # 与 SSE `event: error` 同为 fail-closed；绝不能是正常 message_stop。
                _is_error = (status != 200
                             or any(e.get('type') == 'error' for e in evs))
                check(label + '：流式必须报错，不能正常 message_stop，分片=%d' % size,
                      _is_error and not any(e.get('type') == 'message_stop' for e in evs),
                      (status, raw[-250:]))
                status, raw = post(False)
                check(label + '：非流式必须 502，分片=%d' % size, status == 502, (status, raw[-250:]))
        CURRENT.update(text=GOOD, size=3)
        status, raw = post(True)
        evs = events(raw)
        args = ''.join(e['delta']['partial_json'] for e in evs if e.get('type') == 'content_block_delta' and e['delta']['type'] == 'input_json_delta')
        check('完整双重编码工具仍正常恢复对象', status == 200 and json.loads(args) == ARGS)
        check('真实工具返回 tool_use，随后 message_stop', evs[-2]['delta']['stop_reason'] == 'tool_use' and evs[-1]['type'] == 'message_stop')
        check('真实国际区 Anthropic 路由启用结构化历史', CURRENT['requests'][-1].get('_qd_structured') is True, CURRENT['requests'][-1].get('_qd_structured'))
        for plain in ('这是普通回复。', '格式示例：\n```text\n[Bash 工具调用]\n{"command":"echo demo"}\n```', '[Undeclared 工具调用]\n{"x":1}'):
            CURRENT.update(text=plain, size=2)
            status, raw = post(True)
            check('普通文本、围栏示例或未声明名称保持正常', status == 200 and events(raw)[-1]['type'] == 'message_stop', raw[-200:])
    finally:
        server.shutdown()
        server.server_close()

check('测试无真实出网', not network_attempts, network_attempts)
finish()
