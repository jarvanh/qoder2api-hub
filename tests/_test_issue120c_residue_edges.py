"""#120c：回读成功后的剩余文本 / 流末 tail 也不能带协议残片。

第二轮审计（hub 上的 Ultimate 模型审 qoder_proxy.py）指出两处漏点：
  1. `_sse_recover_dsml` 末尾 `if tail: yield content=tail` 无条件补发 ——
     截在 `<|DSML|inv` 中间、或第二个 DSML 块的头部会从这里漏出去。
  2. 回读成功后 `if clean: yield content=clean` 没有复查残差 ——
     嵌套/重复块剥离不干净时残片同样进正文。
另加一条反向保护：非流式侧判定前先剥代码围栏，避免把「讲解协议的示例」
判成泄漏（误伤会变成无意义的 502）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])


def _frame(delta, finish=None):
    return ("data: " + json.dumps({"id": "c", "model": "m",
                                   "choices": [{"index": 0, "delta": delta,
                                                "finish_reason": finish}]},
                                  ensure_ascii=False) + "\n\n").encode()


def _run(frames, allowed=("write_file",)):
    out, err = [], None
    try:
        for f in P.recover_leaked_tool_calls(iter(frames), set(allowed)):
            out.append(f)
    except Exception as e:            # noqa: BLE001
        err = e
    body = ""
    calls = 0
    for f in out:
        d = P.strip_data_prefix(f.decode("utf-8", "replace"))
        if not d or d == "[DONE]":
            continue
        try:
            o = json.loads(d)
        except Exception:
            continue
        ch = (o.get("choices") or [{}])[0]
        dd = ch.get("delta") or {}
        body += dd.get("content") or ""
        calls += len(dd.get("tool_calls") or [])
    return body, calls, err


CALL = (P.LEAK_MARKER + "\n"
        "<|DSML|calls>\n"
        '<|DSML|invoke name="write_file">\n'
        '<|DSML|parameter name="path">/tmp/a.txt</|DSML|parameter>\n'
        '<|DSML|parameter name="content">hi</|DSML|parameter>\n'
        "</|DSML|invoke>\n"
        "</|DSML|calls>\n")

# ---------------- 1) 判定函数本身
check("#120c 残片尾巴被判为「该丢」",
      P._residue_text_only('<|DSML|invoke name="write_file">') is True)
check("#120c 普通散文不被判为残片",
      P._residue_text_only("这一段是正常说明，没有协议结构。") is False)
check("#120c 空壳垃圾交给 garbage 分支（不在这里丢）",
      P._residue_text_only(P.LEAK_MARKER + "\n<terminal></terminal>\n") is False)

# ---------------- 2) 回读成功 + 尾巴带残片：调用要恢复，残片不能进正文
tail_text = CALL + P.LEAK_MARKER + "\n<|DSML|invoke name=\"write_file\">"
body, calls, err = _run([
    _frame({"role": "assistant", "content": tail_text[:len(tail_text) // 2]}),
    _frame({"role": "assistant", "content": tail_text[len(tail_text) // 2:]}),
    _frame({}, "stop"),
])
check("#120c 尾巴带残片时调用仍能恢复 1 个", calls == 1, calls)
check("#120c 已发出的正文里不得出现 DSML 残片", "DSML" not in body, body[:80])
check("#120c 已发出的正文里不得出现泄漏标记", P.LEAK_MARKER not in body, body[:80])

# ---------------- 3) 代码围栏里的协议示例不算残片（防误伤）
fenced = ("DSML 的工具调用格式长这样：\n"
          "```\n"
          "<|DSML|calls>\n"
          '<|DSML|invoke name="write_file">\n'
          "</|DSML|invoke>\n"
          "</|DSML|calls>\n"
          "```\n"
          "注意参数必须合法。")
check("#120c 围栏剥掉后不再命中 DSML 残片",
      P._dsml_residue_present(P._outside_fences(fenced)) is False)
check("#120c 未剥围栏时会命中（证明这层剥壳是必要的）",
      P._dsml_residue_present(fenced) is True)

finish()
