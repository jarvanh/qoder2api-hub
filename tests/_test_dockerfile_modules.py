"""npm-free 守卫：Dockerfile 必须 COPY 到所有被 import 的 qoder_* 模块（#142 回归）。"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["Dockerfile", "qoder_proxy.py"])

_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
_docker = io.open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()

print("[docker] 镜像里的模块必须与应用导入一致")
_mods = sorted(set(re.findall(r"^import (qoder_[a-z_]+)", _src, re.M)))
check("至少导入 10 个模块", len(_mods) >= 10, len(_mods))
_missing = [m for m in _mods if (m + ".py") not in _docker]
check("Dockerfile 覆盖所有被 import 的 qoder_* 模块", _missing == [], _missing)

print("[docker] 新增模块（#142 的教训）")
check("qoder_keepalive.py 在 COPY 列表里", "qoder_keepalive.py" in _docker)
check("smoke 检查用的是「按 import 推导」，不需要手维护清单",
      "grep -hoE" in io.open(
          os.path.join(ROOT, ".github", "workflows", "docker-publish.yml"),
          encoding="utf-8").read())

print("[docker] COPY 列表里的每个文件都存在")
_blocks = re.findall(r"&#94;COPY (.*?)(?=\n(?:\w|#)|\n\n)", _docker, re.S)
_files = []
for blk in re.findall(r"^COPY (.*?)(?=\n\w)", _docker, re.S | re.M):
    # 去掉行尾注释，避免把中文说明当成文件名
    blk = re.sub(r"#.*", "", blk)
    for tok in blk.replace("\\", " ").split():
        if tok.endswith((".py", ".json", ".html")) and not tok.startswith("/") \
                and tok not in ("不在镜像内，", "不在镜像内"):
            _files.append(tok)
_miss_files = [f for f in _files if not os.path.exists(os.path.join(ROOT, f))]
check("COPY 引用的文件都存在", _miss_files == [], _miss_files)

finish()
