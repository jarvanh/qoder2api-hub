"""Runtime settings for the Qoder gateway: panel password and API keys.

Everything lives in `accounts/settings.json` so a change made from the web
panel survives a restart without editing the launcher .bat files. The panel
password is never stored in clear text - only a PBKDF2-SHA256 digest.

Only the Python standard library is required.
"""

import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import tempfile
import time
from pathlib import Path

# fork：沿用上游 v1.10.2 的密码域语义——密码只存 PBKDF2 摘要，首次安装需在
# 服务器终端 --set-panel-password 首设；不再生成明文密码文件（上游已移除）。
PBKDF2_ROUNDS = 120_000
SESSION_TTL = 7 * 24 * 3600

_lock = threading.RLock()


def settings_path(accounts_dir):
    return os.path.join(accounts_dir, "settings.json")


def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def _atomic_write_text(path, text, mode=0o600):
    """Write `text` atomically with `mode` from the moment the tmp file exists.

    mkstemp() creates a unique file with 0600 from the first byte, so concurrent
    writers (panel + CLI + tasks) never share a tmp path and a credential is
    never readable by other users during the write window. rename() publishes
    the complete file atomically.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".",
                               suffix=".tmp")
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load(accounts_dir):
    """Return the persisted settings, or an empty dict on a fresh install."""
    path = settings_path(accounts_dir)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def save(accounts_dir, data):
    """Atomic 0600 write so a crash cannot leave a half-written settings file."""
    with _lock:
        base = Path(accounts_dir).resolve()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        path = base / "settings.json"
        # fork 加固（2026-11-14）：0600 权限 + 防路径逃逸，写法沿用 fork 的
        # _atomic_write_text；上游 v1.10.2 的 mkstemp 写法语义相同但不带 mode。
        tmp = base / "settings.json.tmp"
        if not (path.is_relative_to(base) and tmp.is_relative_to(base)):
            raise ValueError("path escapes base directory")
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2),
                           mode=0o600)
        return str(path)


PANEL_PASSWORD_MIN_LENGTH = 12


def _panel_password_record(accounts_dir):
    data = load(accounts_dir)
    return _parse_panel_password_record(data)


def _parse_panel_password_record(data):
    stored = data.get("panel_password_hash")
    salt = data.get("panel_password_salt")
    rounds = data.get("panel_password_rounds", PBKDF2_ROUNDS)
    if (data.get("panel_password_default") is True
            or not isinstance(stored, str) or not re.fullmatch(r"[0-9a-f]{64}", stored)
            or not isinstance(salt, str) or not re.fullmatch(r"[0-9a-f]{32}", salt)
            or type(rounds) is not int or not 1 <= rounds <= 2_000_000):
        return None
    return stored, salt, rounds


def panel_password_binding(accounts_dir):
    record = _panel_password_record(accounts_dir)
    return record[0] if record else ""


def panel_password_is_default(accounts_dir):
    """True when no valid panel password digest is stored.

    上游 v1.10.2 改名说明：这里保留 fork 的「无摘要即默认」语义；上游把
    panel_password_default 标记视为「待本地开通」并锁面板，fork 认为旧
    settings 里的该标记不该复活 admin 后门，故忽略该标记（见 verify）。
    """
    return not panel_password_binding(accounts_dir)


def verified_panel_password_binding(accounts_dir, password):
    """Return the exact credential revision that was verified, or empty on failure."""
    if (not isinstance(password, str) or len(password) > 1024
            or not password.strip()):
        return ""
    record = _panel_password_record(accounts_dir)
    if not record:
        return ""
    stored, salt, rounds = record
    try:
        given = _digest(password, salt, rounds)
    except (UnicodeError, ValueError):
        return ""
    return stored if hmac.compare_digest(given, stored) else ""


def verify_panel_password(accounts_dir, password):
    return bool(verified_panel_password_binding(accounts_dir, password))


def set_panel_password(accounts_dir, password, *, expected_binding=None):
    """Set an explicit password; always stored as a salted PBKDF2 digest.

    上游 v1.10.2+：新增 expected_binding 乐观锁与 12–1024 长度下限；
    fork 收紧语义（无 DEFAULT_PANEL_PASSWORD 提权路径，见 verified_*）。
    """
    password = str(password or "")
    if (not PANEL_PASSWORD_MIN_LENGTH <= len(password) <= 1024
            or not password.strip()):
        raise ValueError("panel password must be 12-1024 characters long")
    try:
        salt = secrets.token_hex(16)
        digest = _digest(password, salt)
    except UnicodeError as exc:
        raise ValueError("invalid characters in panel password") from exc
    with _lock:
        data = _load_for_update(accounts_dir)
        record = _parse_panel_password_record(data)
        if expected_binding is not None and (not record or expected_binding != record[0]):
            raise ValueError("panel password changed; sign in again")
        data.update(panel_password_salt=salt, panel_password_rounds=PBKDF2_ROUNDS,
                    panel_password_hash=digest, panel_password_default=False)
        save(accounts_dir, data)
    return digest


def api_key_override(accounts_dir):
    """Return (key, is_set). `is_set` means the panel manages the key."""
    data = load(accounts_dir)
    if not data.get("api_key_set"):
        return None, False
    return str(data.get("api_key") or ""), True


def set_api_key(accounts_dir, key):
    with _lock:
        data = _load_for_update(accounts_dir)
        data["api_key"] = key or ""
        data["api_key_set"] = True
        save(accounts_dir, data)


def ensure_launcher_key(accounts_dir):
    """Return the persisted LAN key, creating one on first use.

    LAN mode must never ship a well-known default: the gateway spends the
    account's own upstream quota, so anyone on the same network could drain it.
    The value is generated once and stored so clients keep working across
    restarts. Returns (key, created) so the caller can tell the user whether
    this run minted a fresh credential.
    """
    with _lock:
        data = _load_for_update(accounts_dir)
        existing = str(data.get("launcher_key") or "").strip()
        if existing:
            return existing, False
        key = "qd-" + secrets.token_urlsafe(24)
        data["launcher_key"] = key
        save(accounts_dir, data)
        return key, True


# --------------------------------------------------------------- API keys
# Each key can be bound to one upstream realm, so several clients can hit
# different exits at the same time instead of sharing the global switch.

REALMS = ("", "intl", "cn")


# ---------------------------------------------------------------------------
# 模型上下文窗口覆盖（issue #106）
# ---------------------------------------------------------------------------
def model_context(accounts_dir):
    """{model_key: "1M"|"400K"|"200K"|int}：用户为各模型选的上下文档。"""
    raw = load(accounts_dir).get("model_context")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        key = str(k or "").strip()
        if not key:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v > 0:
            out[key] = v
            continue
        label = str(v or "").strip()
        if label:
            out[key] = label
    return out


def set_model_context(accounts_dir, model, label):
    """设置/清除单个模型的上下文档（label 空串 = 回到官方默认）。"""
    with _lock:
        data = _load_for_update(accounts_dir)
        cur = data.get("model_context")
        cur = dict(cur) if isinstance(cur, dict) else {}
        key = str(model or "").strip()
        if not key:
            raise ValueError("model required")
        if label in (None, "", 0):
            cur.pop(key, None)
        elif isinstance(label, int) and not isinstance(label, bool):
            if label <= 0:
                raise ValueError("tokens must be positive")
            cur[key] = int(label)
        else:
            label = str(label).strip()
            if label.isdigit() and int(label) > 0:
                cur[key] = int(label)
            else:
                cur[key] = label[:12]
        data["model_context"] = cur
        save(accounts_dir, data)
        return cur


def tool_echo_mode(accounts_dir):
    """面板可切换的「工具协议残片收尾策略」（issue #135）；默认空=跟随环境/内置。"""
    val = str(load(accounts_dir).get("tool_echo_mode") or "").strip().lower()
    return val if val in ("strict", "lenient") else ""


def set_tool_echo_mode(accounts_dir, mode):
    """写入收尾策略；传空串表示清除（回落到环境变量/内置默认）。"""
    with _lock:
        data = _load_for_update(accounts_dir)
        val = str(mode or "").strip().lower()
        if val in ("strict", "lenient"):
            data["tool_echo_mode"] = val
        else:
            data.pop("tool_echo_mode", None)
        save(accounts_dir, data)
        return tool_echo_mode(accounts_dir)


def anthropic_thinking(accounts_dir):
    """面板/配置文件的全局 Anthropic thinking block 输出策略（issue #146）；默认空=跟随环境变量。"""
    val = str(load(accounts_dir).get("anthropic_thinking") or "").strip().lower()
    return val if val in ("empty", "off") else ""


def set_anthropic_thinking(accounts_dir, mode):
    """设置全局 Anthropic thinking 输出策略；传空串清除（回落到环境变量）。"""
    with _lock:
        data = _load_for_update(accounts_dir)
        val = str(mode or "").strip().lower()
        if val in ("empty", "off"):
            data["anthropic_thinking"] = val
        else:
            data.pop("anthropic_thinking", None)
        save(accounts_dir, data)
        return anthropic_thinking(accounts_dir)


def _clean_account_selectors(value, allow_empty=False):
    """Keep selectors deterministic; malformed restrictions must not fail open."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("account selectors must be lists")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("account selectors must contain strings")
        item = item.strip()
        if (item or allow_empty) and item not in out:
            out.append(item)
    return out


def _clean_key_entry(entry):
    """Normalize one stored key entry; returns None when unusable."""
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    if not key:
        return None
    realm = str(entry.get("realm") or "").strip().lower()
    if realm not in REALMS:
        realm = ""
    return {
        "id": str(entry.get("id") or secrets.token_hex(6)),
        "name": str(entry.get("name") or "").strip() or "未命名",
        "key": key,
        "realm": realm,
        "enabled": entry.get("enabled", True) is not False,
        "created_at": entry.get("created_at") or time.strftime("%Y/%m/%d %H:%M"),
        # P1-4：每 Key 模型白名单（空列表 = 不限制）。**必须显式保留**——
        # 本函数是白名单式重建，漏掉即「保存时被静默丢弃」（limits 同型坑）。
        "models": _clean_model_patterns(entry.get("models")),
        # Empty selectors retain legacy unrestricted keys. A named group that
        # no longer exists remains configured, so scope resolution denies all.
        "account_ids": _clean_account_selectors(entry.get("account_ids")),
        "account_groups": _clean_account_selectors(
            entry.get("account_groups") if "account_groups" in entry
            else ([entry["group"]] if "group" in entry else []), allow_empty=True),
        # issue #69：Key 备注（纯展示，不参与鉴权；同样必须显式保留）。
        "note": str(entry.get("note") or "").strip()[:120],
        # issue #146：每 Key Anthropic thinking 策略（empty/off，空串跟随全局）。
        "anthropic_thinking": (str(entry.get("anthropic_thinking") or "").strip().lower()
                               if str(entry.get("anthropic_thinking") or "").strip().lower() in ("empty", "off")
                               else ""),
    }


def api_keys(accounts_dir):
    """Every configured key, newest shape first.

    A settings file written by an older build only has the single
    `api_key`/`api_key_set` pair; that is surfaced as one unbound entry so
    upgrades keep working without a migration step.
    """
    data = load(accounts_dir)
    stored = data.get("api_keys")
    if isinstance(stored, list):
        out = []
        seen = set()
        for raw in stored:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                out.append(entry)
        return out

    if data.get("api_key_set"):
        legacy = str(data.get("api_key") or "").strip()
        if legacy:
            return [{
                "id": "legacy",
                "name": "默认（跟随面板切换）",
                "key": legacy,
                "realm": "",
                "enabled": True,
            }]
    return []


def set_api_keys(accounts_dir, keys):
    """Replace the whole key list. Returns the stored list."""
    with _lock:
        cleaned = []
        seen = set()
        for raw in keys or []:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                cleaned.append(entry)
        data = _load_for_update(accounts_dir)
        data["api_keys"] = cleaned
        # The single-key fields are now derived; drop them so there is one
        # source of truth and the list survives a restart.
        data.pop("api_key", None)
        data.pop("api_key_set", None)
        save(accounts_dir, data)
        return cleaned


def match_api_key(accounts_dir, supplied, extra_keys=()):
    """Find which configured key a request presented, if any.

    Returns a copy of the entry (with a `source` field) so the caller can read
    the bound realm, or None when nothing matches.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    supplied = supplied.encode("utf-8", "surrogatepass")
    for entry in api_keys(accounts_dir):
        if entry["enabled"] and hmac.compare_digest(supplied, entry["key"].encode("utf-8", "surrogatepass")):
            out = dict(entry)
            out["source"] = "panel"
            return out
    for candidate in extra_keys:
        candidate = (candidate or "").strip()
        if candidate and hmac.compare_digest(supplied, candidate.encode("utf-8", "surrogatepass")):
            return {
                "id": "launcher",
                "name": "启动参数",
                "key": candidate,
                "realm": "",
                "enabled": True,
                "source": "launcher",
            }
    return None


def auth_disabled(accounts_dir):
    """True when the operator switched API-key checking off entirely."""
    return load(accounts_dir).get("auth_disabled") is True


def set_auth_disabled(accounts_dir, disabled):
    with _lock:
        data = _load_for_update(accounts_dir)
        data["auth_disabled"] = bool(disabled)
        save(accounts_dir, data)


def omit_default_system_prompt(accounts_dir):
    return load(accounts_dir).get("omit_default_system_prompt") is True


def identity_guard(accounts_dir):
    """出站身份句守卫开关（面板「设置」/ QD_IDENTITY_GUARD）。

    开启后对出站 messages 中命中上游品牌检测的身份句做最小字符级
    中和（详见 qoder_identity_guard 模块文档）。默认关闭。
    """
    return load(accounts_dir).get("identity_guard") is True


def set_identity_guard(accounts_dir, enabled):
    with _lock:
        data = _load_for_update(accounts_dir)
        data["identity_guard"] = bool(enabled)
        save(accounts_dir, data)


def set_omit_default_system_prompt(accounts_dir, enabled):
    with _lock:
        data = _load_for_update(accounts_dir)
        data["omit_default_system_prompt"] = enabled
        save(accounts_dir, data)


def web_tools_environment():
    """Parsed enable override and raw normalized values for runtime and panel."""
    enabled = (os.environ.get("QD_WEB_TOOLS") or "").strip().lower()
    backend = (os.environ.get("QD_WEB_TOOLS_BACKEND") or "").strip().lower()
    if enabled in ("1", "on", "true", "yes", "enable", "enabled"):
        return True, enabled, backend
    if enabled in ("0", "off", "false", "no", "disable", "disabled"):
        return False, enabled, backend
    return None, "", backend


def _load_for_update(accounts_dir):
    """A failed read is not a fresh install; never replace unreadable settings."""
    try:
        with open(settings_path(accounts_dir), encoding="utf-8-sig") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise OSError("settings.json is not valid text; refusing to overwrite it") from exc
    if not text.strip():
        return {}      # no content at all: nothing can be lost by starting fresh
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise OSError("settings.json is invalid; refusing to overwrite it") from exc
    if not isinstance(data, dict):
        raise OSError("settings.json must contain an object; refusing to overwrite it")
    return data


def web_tools_enabled(accounts_dir):
    """本地 web 工具开关（面板「设置 → 本地 web 工具」）。

    默认关闭：这是唯一会**由模型驱动出网**的能力，必须显式开启。
    环境变量 QD_WEB_TOOLS 优先于这里的持久化开关。
    """
    return load(accounts_dir).get("web_tools") is True


def set_web_tools_enabled(accounts_dir, enabled):
    set_web_tools_config(accounts_dir, {"enabled": bool(enabled)})


def web_tools_config(accounts_dir):
    """Persisted backend and separate provider profiles; internal, includes keys."""
    return _web_tools_config(load(accounts_dir))


def _web_tools_config(data):
    import qoder_external_web as external
    provider = data.get("web_tools_external_provider") or "searxng"
    if provider not in external.PROVIDERS:
        provider = "searxng"
    saved = data.get("web_tools_external_profiles") or {}
    profiles = {name: external.normalize(saved.get(name) or {}, name)
                for name in external.PROVIDERS}
    return {"backend": data.get("web_tools_backend") or "local",
            "external": profiles[provider], "external_profiles": profiles,
            "fetch_backend": data.get("web_fetch_backend") or "inherit",
            "external_fetch": external.normalize_fetch(data.get("web_fetch_external") or {})}


def set_web_tools_config(accounts_dir, updates):
    """Validate the whole patch before atomically persisting any field."""
    import qoder_external_web as external
    if not isinstance(updates, dict):
        raise ValueError("web tools settings must be an object")
    if set(updates) - {"enabled", "backend", "external", "fetch_backend", "external_fetch"}:
        raise ValueError("unknown web tools setting")
    with _lock:
        data = _load_for_update(accounts_dir)
        enabled_env, _, backend_env = web_tools_environment()
        if "enabled" in updates:
            if type(updates["enabled"]) is not bool:
                raise ValueError("enabled must be a boolean")
            if enabled_env is None:
                data["web_tools"] = updates["enabled"]
        if "backend" in updates:
            if updates["backend"] not in ("local", "qoder", "external"):
                raise ValueError("backend must be local, qoder or external")
            if not backend_env:
                data["web_tools_backend"] = updates["backend"]
        if "external" in updates:
            patch = updates["external"]
            if not isinstance(patch, dict):
                raise ValueError("external search configuration must be an object")
            provider = patch.get("provider") or data.get("web_tools_external_provider") or "searxng"
            profiles = dict(data.get("web_tools_external_profiles") or {})
            # Keys omitted by a masked form are retained only for this provider.
            profile = dict(profiles.get(provider) or {})
            unknown = set(patch) - set(external.defaults(provider))
            if unknown:
                raise ValueError("unknown external search setting")
            old_origin = external.endpoint_origin(external.normalize(profile, provider)["endpoint"])
            profile.update(patch)
            profile = external.normalize(profile, provider)
            if "api_key" not in patch and old_origin != external.endpoint_origin(profile["endpoint"]):
                profile["api_key"] = ""
            profiles[provider] = profile
            data["web_tools_external_profiles"] = profiles
            data["web_tools_external_provider"] = provider
        if "fetch_backend" in updates:
            if updates["fetch_backend"] not in ("inherit", "local", "qoder", "external"):
                raise ValueError("fetch_backend must be inherit, local, qoder or external")
            data["web_fetch_backend"] = updates["fetch_backend"]
        if "external_fetch" in updates:
            patch = updates["external_fetch"]
            if not isinstance(patch, dict) or set(patch) - set(external.FETCH_DEFAULTS):
                raise ValueError("invalid external fetch settings")
            fetch_profile = dict(data.get("web_fetch_external") or {})
            old_endpoint = fetch_profile.get("endpoint") or ""
            fetch_profile.update(patch)
            fetch_profile = external.normalize_fetch(fetch_profile)
            if "api_key" not in patch and "endpoint" in patch:
                # Validate the previous endpoint independently of other old draft fields.
                old_endpoint = external.normalize_fetch({"endpoint": old_endpoint})["endpoint"]
                if external.endpoint_origin(old_endpoint) != external.endpoint_origin(fetch_profile["endpoint"]):
                    fetch_profile["api_key"] = ""
            data["web_fetch_external"] = fetch_profile
        config = _web_tools_config(data)
        enabled = data.get("web_tools") is True if enabled_env is None else enabled_env
        backend = backend_env or config["backend"]
        if enabled:
            if backend not in ("local", "qoder", "external"):
                raise ValueError("QD_WEB_TOOLS_BACKEND must be local, qoder or external")
            if backend == "external":
                external.normalize(config["external"], ready=True)
            if config["fetch_backend"] == "external":
                external.normalize_fetch(config["external_fetch"], ready=True)
        save(accounts_dir, data)
    return config


# ------------------------------------------------------------- model gates
# P1-4：模型闸门配置层。全局封禁（banned_models）与每 Key 白名单（key 条目的
# models 字段）共用同一套 pattern 清洗与匹配：fnmatch 通配、大小写不敏感；
# **默认空 = 不封禁 / 不限制**（未碰这两个配置的安装行为与改动前一致）。
_MODEL_SPLIT = re.compile(r"[,;\n]")


def _clean_model_patterns(value):
    """把 字符串 / 列表 / 集合 清洗成 pattern 列表（strip+lower+去重+去空）。

    非法类型返回 []（fail-open：读配置永远不抛）。
    """
    if isinstance(value, str):
        raw = _MODEL_SPLIT.split(value)
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out = []
    for item in raw:
        pattern = str(item or "").strip().lower()
        if pattern and pattern not in out:
            out.append(pattern)
    return out


def key_allows_model(entry, model):
    """True = 该 Key 未设模型限制，或 model 命中它的 models 列表。

    - 空列表（或字段缺失）= 不受限（未碰过这个字段的安装行为不变）；
    - 受限 Key 且 model 为空 = **拒绝**（wb 同语义：空模型没有可判定依据，
      放行只会带着空模型白跑一趟上游）；
    - 匹配 fnmatch（大小写已在清洗时统一为小写），支持 gpt-4* 这类通配。
    """
    patterns = _clean_model_patterns((entry or {}).get("models"))
    if not patterns:
        return True
    name = str(model or "").strip().lower()
    if not name:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def banned_models(accounts_dir):
    """全局封禁的模型 pattern 列表（settings 键 banned_models；默认空）。"""
    with _lock:
        raw = load(accounts_dir).get("banned_models")
    return _clean_model_patterns(raw)


def set_banned_models(accounts_dir, value):
    """落盘全局封禁列表（清洗后存储；空 = 不封禁任何模型）。返回清洗结果。"""
    patterns = _clean_model_patterns(value)
    with _lock:
        data = _load_for_update(accounts_dir)
        data["banned_models"] = patterns
        save(accounts_dir, data)
    return patterns


# ------------------------------------------------------- panel sessions
PANEL_SECRET_KEY = "panel_session_secret"
_SAFE_TOKEN_PART = re.compile(r"^[A-Za-z0-9_-]+$")


def panel_session_secret(accounts_dir):
    """面板会话签名密钥：首次生成后持久化，重启后旧 token 仍能验证。

    早期 PanelSessions 把 token 存在进程内存里，网关一重启（launchd 拉活、
    崩溃自愈、升级后 restart）浏览器就得重新登录——「更安全」的默认值在
    本机托管 + 频繁重启的场景下只换来重复登录。密钥与其它凭证同存
    accounts/settings.json（该目录本来就是 0600 级的凭证库），不降低等级。

    settings.json 存在但解析不了时**拒绝回写**（抛异常，由调用方退到进程内
    临时密钥）：宁可面板多登一次，也不能把密码哈希和 API Key 一起抹掉。
    """
    with _lock:
        data = _load_for_update(accounts_dir)
        secret = str(data.get(PANEL_SECRET_KEY) or "")
        if len(secret) >= 32:
            return secret
        secret = secrets.token_urlsafe(48)
        data[PANEL_SECRET_KEY] = secret
        save(accounts_dir, data)
        return secret


class PanelSessions(object):
    """签名式面板会话 token，**重启后仍然有效**。

    token 自校验，服务端不存会话表：`<过期秒>.<nonce>.<hmac_sha256>`。
    验证时使用持久化密钥和当前密码摘要。同一密码版本跨重启有效；
    更换密码或签名密钥都会令旧会话失效。旧版签名格式不再接受。

    已知取舍：revoke(token) 的黑名单只在**本进程**生效。因此「退出登录」端点
    改调 revoke_all()（换密钥）——否则重启后那个已退出的 token 会复活到 TTL
    结束（比旧实现更差：旧实现重启把所有 token 一起作废）。
    """

    def __init__(self, accounts_dir=None, ttl=SESSION_TTL):
        # accounts_dir 接受 callable：qoder_proxy 在模块顶层就能构造 PANEL，
        # 而 ACCOUNTS_DIR 要到后面解析 --accounts-dir 才定下来。
        self.ttl = ttl
        self._dir = accounts_dir
        self._secret = None
        self._revoked = {}
        self._ephemeral = False     # 密钥未能落盘（退到进程内临时密钥）
        self._lock = threading.RLock()

    def _accounts_dir(self):
        d = self._dir
        if callable(d):
            d = d()
        return d or ""

    def _key(self):
        if self._secret:
            return self._secret
        with self._lock:
            if not self._secret:
                try:
                    self._secret = panel_session_secret(self._accounts_dir())
                except Exception:
                    # settings.json 读不出/写不进（解析损坏、只读目录、磁盘满）：
                    # 退回**进程内**临时密钥。面板退化成「重启需重新登录」，
                    # 而不是所有登录都 500，也不是把凭证文件抹掉。
                    self._secret = secrets.token_urlsafe(48)
                    self._ephemeral = True
            return self._secret

    def _sign(self, expiry, nonce, binding=None):
        if binding is None:
            binding = panel_password_binding(self._accounts_dir())
        # Domain change deliberately invalidates all sessions issued before
        # the credential-binding policy, including legacy default sessions.
        message = "panel-v2:%s:%d.%s" % (binding, expiry, nonce)
        return hmac.new(self._key().encode("utf-8"), message.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def create(self, binding=None):
        expiry = int(time.time()) + self.ttl
        nonce = secrets.token_urlsafe(12)
        return "%d.%s.%s" % (expiry, nonce, self._sign(expiry, nonce, binding))

    def valid(self, token, binding=None):
        if not token:
            return False
        parts = str(token).split(".")
        if len(parts) != 3:
            return False
        expiry_s, nonce, signature = parts
        # 先做字符/长度白名单：hmac.compare_digest 遇到非 ASCII 会抛
        # TypeError（"comparing strings with non-ASCII characters…"），
        # 一个粘贴坏掉的 token 就能把连接打断 500/断连，而不是干净地 401。
        if not expiry_s.isdigit() or len(expiry_s) > 12:
            return False
        if not (1 <= len(nonce) <= 64 and _SAFE_TOKEN_PART.match(nonce)):
            return False
        if len(signature) != 64 or not _SAFE_TOKEN_PART.match(signature):
            return False
        expiry = int(expiry_s)
        if expiry < time.time():
            return False
        if not hmac.compare_digest(signature, self._sign(expiry, nonce, binding)):
            return False
        with self._lock:
            return token not in self._revoked

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            now = time.time()
            if len(self._revoked) > 256:      # 惰性清理，避免无限增长
                self._revoked = {k: v for k, v in self._revoked.items() if v > now}
            self._revoked[token] = now + self.ttl

    def revoke_all(self):
        """换密钥 -> 所有已发 token 立即失效（改密码后的踢出语义靠这个）。

        文件 IO 放在锁外：这把锁的语义是「保护几微秒的内存查询」，而 save()
        要 mkdir+write+replace（还再取一次模块级 _lock）。带着它做磁盘写会让
        并发进来的每个 valid() 排队等 IO。新密钥先算好、锁内只换内存里的值，
        落盘失败也不影响本次进程内的作废效果。
        """
        new_secret = secrets.token_urlsafe(48)
        accounts_dir = self._accounts_dir()
        with self._lock:
            self._secret = new_secret
            self._revoked.clear()
        try:
            with _lock:
                data = _load_for_update(accounts_dir)
                data[PANEL_SECRET_KEY] = new_secret
                save(accounts_dir, data)
        except Exception:
            # 写盘失败：本进程已换密钥（旧 token 立即作废）；重启后读回旧密钥
            # 会让旧 token 复活，但这已经优于「改密码却踢不掉任何会话」。
            pass


# ---------------------------------------------------------------- limits
# 四个守卫共享同一形状：一个覆盖双区域的 global 默认值 + 可选的 per-realm
# override。**留空 = 继承 global**（与显式 0 = "关闭"严格区分）；从不碰这份
# 配置的安装，行为与没有它完全一致。形状照抄 wb_settings.py 的三条读/写入口
# + 分组 map，但 qoder 侧 **默认值全部为 0（关闭）**——量纲尚未核对
# （.team/_gap/03-candidate-deep-dive.md R4），不给任何非零默认。
LIMIT_KEYS = ("reserve_credits", "daily_token_limit",
              "daily_credit_limit", "model_daily_token_limit",
              "expiring_window_days")
LIMIT_REALMS = ("intl", "cn")
LIMIT_SCOPES = ("global",) + LIMIT_REALMS
LIMITS_KEY = "limits"


def _empty_limit_entry():
    """一个守卫的空条目：global 默认 0（关），两个 realm 槽位 None=继承。"""
    return {"global": 0, "intl": None, "cn": None}


def _coerce_global(value):
    """global 阈值：垃圾值与负数收敛到 0（关）。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    return max(0, number)


def _coerce_override(value):
    """per-realm override：None/空串="继承 global"（与显式 0="本区域关闭"不同）。"""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _normalize_limits(raw):
    """把存储的 map 补全成完整形状（部分/手改的 settings.json 也安全读）。"""
    limits = {}
    for key in LIMIT_KEYS:
        entry = raw.get(key)
        if not isinstance(entry, dict):
            entry = {}
        global_raw = entry.get("global")
        # 缺省 global → 0（关）；显式 0 是真实的"关"，不会被误重新打开。
        limits[key] = {
            "global": 0 if global_raw is None else _coerce_global(global_raw),
            "intl": _coerce_override(entry.get("intl")),
            "cn": _coerce_override(entry.get("cn")),
        }
    return limits


def limits_data(accounts_dir):
    """分组后的 limits map（每个守卫都补全为完整形状）。

    qoder 从未有过旧扁平键，因此不做 wb 的 _fold_legacy_limits 迁移分支。
    """
    with _lock:
        raw = load(accounts_dir).get(LIMITS_KEY)
        return _normalize_limits(raw if isinstance(raw, dict) else {})


def limit_value(accounts_dir, key, realm=None):
    """单守卫、单区域的**生效值**。

    realm 为 intl/cn 且有 override 时用 override，否则回落 global；
    未知 key 读作 0（关），不卡请求路径（与 wb 同语义）。
    """
    entry = limits_data(accounts_dir).get(key) or _empty_limit_entry()
    if realm in LIMIT_REALMS:
        override = entry.get(realm)
        if override is not None:
            return override
    return entry.get("global") or 0


def limit_values(accounts_dir, key):
    """单守卫 → {"global": g, "intl": ..., "cn": ...}（intl/cn 已把 global 填好）。

    热路径上每个账号按自己的 realm 取一次即可；保证三键齐全且 intl/cn 非
    None（验收 A5）。
    """
    entry = limits_data(accounts_dir).get(key) or _empty_limit_entry()
    global_value = entry.get("global") or 0
    values = {"global": global_value}
    for realm in LIMIT_REALMS:
        override = entry.get(realm)
        values[realm] = global_value if override is None else override
    return values


def set_limit(accounts_dir, key, scope, value):
    """落盘单个守卫的单个作用域；返回该守卫的完整条目。

    key 不在 LIMIT_KEYS、scope 不在 LIMIT_SCOPES 时抛 ValueError；
    scope="global" 恒存数字（None/垃圾→0）；intl/cn 传 None/空串 =
    清除 override 回到继承。
    """
    if key not in LIMIT_KEYS:
        raise ValueError("unknown limit: %s" % key)
    if scope not in LIMIT_SCOPES:
        raise ValueError("unknown scope: %s" % scope)
    with _lock:
        data = _load_for_update(accounts_dir)
        raw = data.get(LIMITS_KEY)
        limits = _normalize_limits(raw if isinstance(raw, dict) else {})
        entry = limits[key]
        if scope == "global":
            entry["global"] = _coerce_global(value)
        else:
            entry[scope] = _coerce_override(value)
        data[LIMITS_KEY] = limits
        save(accounts_dir, data)
    return entry
