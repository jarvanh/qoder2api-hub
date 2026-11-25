"""Admin-configured web APIs. Model input never selects endpoints or headers."""
import copy
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from qoder_web_response import clean_text, public_link

PROVIDERS = ("searxng", "tavily", "brave", "serper", "custom")
_BASE = {"endpoint": "", "method": "GET", "query_field": "q", "limit_field": "",
         "params": {}, "results_path": "results", "title_field": "title",
         "url_field": "url", "snippet_field": "snippet", "api_key": "",
         "auth_header": "Authorization", "auth_prefix": "Bearer"}
_PRESETS = {
    "searxng": {"params": {"format": "json"}, "snippet_field": "content"},
    "tavily": {"endpoint": "https://api.tavily.com/search", "method": "POST",
               "query_field": "query", "limit_field": "max_results",
               "params": {"search_depth": "basic", "include_answer": False,
                          "include_raw_content": False}, "snippet_field": "content"},
    "brave": {"endpoint": "https://api.search.brave.com/res/v1/web/search",
              "limit_field": "count", "results_path": "web.results",
              "snippet_field": "description", "auth_header": "X-Subscription-Token",
              "auth_prefix": ""},
    "serper": {"endpoint": "https://google.serper.dev/search", "method": "POST",
               "limit_field": "num", "results_path": "organic", "url_field": "link",
               "auth_header": "X-API-KEY", "auth_prefix": ""},
    "custom": {},
}
FETCH_DEFAULTS = {"endpoint": "", "method": "POST", "url_field": "url", "params": {},
                  "text_path": "text", "title_path": "title", "final_url_path": "final_url",
                  "api_key": "", "auth_header": "Authorization", "auth_prefix": "Bearer"}


def defaults(provider):
    if provider not in PROVIDERS:
        raise ValueError("unsupported external search provider")
    return dict(copy.deepcopy(_BASE), **copy.deepcopy(_PRESETS[provider]), provider=provider)


def normalize(profile, provider=None, ready=False):
    if not isinstance(profile, dict):
        raise ValueError("external search configuration must be an object")
    provider = provider or profile.get("provider") or "searxng"
    result = defaults(provider)
    for key in _BASE:
        if key in profile:
            result[key] = copy.deepcopy(profile[key])
    _normalize_http_profile(result, "search", ("query_field", "limit_field"),
                            ("results_path", "title_field", "url_field", "snippet_field"), ready)
    if ready and provider in ("tavily", "brave", "serper") and not result["api_key"]:
        raise ValueError("external search API key is required for %s" % provider)
    return result


def normalize_fetch(profile, ready=False):
    if not isinstance(profile, dict):
        raise ValueError("external fetch configuration must be an object")
    result = {key: copy.deepcopy(profile.get(key, default)) for key, default in FETCH_DEFAULTS.items()}
    _normalize_http_profile(result, "fetch", ("url_field",),
                            ("text_path", "title_path", "final_url_path"), ready)
    for key in ("text_path", "final_url_path"):
        if not result[key]:
            raise ValueError("external fetch %s is required" % key)
    return result


def endpoint_origin(endpoint):
    """Compare validated endpoint origins before reusing a stored credential."""
    if not endpoint:
        return None
    parsed = urllib.parse.urlsplit(endpoint)
    host = parsed.hostname
    from ipaddress import ip_address
    try:
        host = str(ip_address(host))
    except ValueError:
        # Keep trailing dots and different hostname spellings distinct.
        host = host.lower()
    port = parsed.port if parsed.port is not None else {"http": 80, "https": 443}[parsed.scheme]
    return parsed.scheme, host, port


def _normalize_http_profile(result, label, input_fields, output_fields, ready):
    prefix = "external " + label
    for key in result:
        if key != "params" and not isinstance(result[key], str):
            raise ValueError("%s %s must be text" % (prefix, key))
    for key in ("endpoint", "method", "auth_header", "auth_prefix") + input_fields + output_fields:
        result[key] = result[key].strip()
    result["method"] = result["method"].upper()
    if result["method"] not in ("GET", "POST"):
        raise ValueError(prefix + " method must be GET or POST")
    endpoint = result["endpoint"]
    if endpoint:
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            _ = parsed.port
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and parsed.password is None and not parsed.fragment
                     and not any(c.isspace() or ord(c) < 32 for c in endpoint) and "\\" not in endpoint)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(prefix + " endpoint must be an HTTP(S) URL without credentials or fragment")
    elif ready:
        raise ValueError(prefix + " endpoint is required")
    for key in input_fields:
        if (not result[key] and key != "limit_field") or (result[key] and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", result[key])):
            raise ValueError("invalid %s %s" % (prefix, key))
    for key in output_fields:
        if result[key] and not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", result[key]):
            raise ValueError("invalid %s %s (use dot-separated field names)" % (prefix, key))
    header = result["auth_header"]
    if (header and not re.fullmatch(r"[A-Za-z0-9-]{1,80}", header)) or header.lower() in (
            "host", "connection", "content-length", "transfer-encoding", "proxy-authorization"):
        raise ValueError("invalid " + prefix + " authentication header")
    for key in ("api_key", "auth_prefix"):
        if len(result[key]) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in result[key]):
            raise ValueError("invalid %s %s" % (prefix, key))
    if not isinstance(result["params"], dict):
        raise ValueError(prefix + " parameters must be a JSON object")
    try:
        serialized = json.dumps(result["params"], allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError("invalid " + prefix + " parameters") from None
    if len(serialized) > 16000:
        raise ValueError(prefix + " parameters are too large")


def public_profile(profile):
    result = copy.deepcopy(profile)
    result["api_key_set"] = bool(result.pop("api_key", ""))
    return result


def field(value, path):
    for part in path.split(".") if path else []:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json_request(endpoint, method, params, headers):
    # The endpoint is explicitly configured by the authenticated administrator.
    # Private/local services are supported, but redirects are not followed, so
    # authentication is never forwarded to an unconfigured destination.
    data = None
    if method == "GET":
        encoded = urllib.parse.urlencode(params, doseq=True)
        endpoint += ("&" if "?" in endpoint else "?") + encoded
    else:
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers = dict(headers, **{"Content-Type": "application/json"})
    request = urllib.request.Request(endpoint, data=data, headers=headers, method=method)
    # Explicit tool proxy only. Local Docker services never inherit host proxies.
    import os
    proxy = (os.environ.get("QD_WEB_TOOLS_PROXY") or "").strip()
    host = urllib.parse.urlsplit(endpoint).hostname or ""
    import ipaddress
    try:
        private = not ipaddress.ip_address(host).is_global
    except ValueError:
        private = "." not in host or host.endswith((".local", ".localhost"))
    proxies = {"http": proxy, "https": proxy} if proxy and not private else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _NoRedirect())
    with opener.open(request, timeout=15) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("external web response is too large")
        return json.loads(raw.decode("utf-8"))


def _headers(profile):
    headers = {"Accept": "application/json", "User-Agent": "qoder2api-web-tools"}
    if profile["api_key"] and profile["auth_header"]:
        headers[profile["auth_header"]] = " ".join(part for part in (profile["auth_prefix"], profile["api_key"]) if part)
    return headers


def search(profile, query, limit=5):
    provider = profile.get("provider") or "custom"
    try:
        profile = normalize(profile, ready=True)
        limit = max(1, min(20, int(limit)))
        query = str(query or "").strip()
        if not query or len(query) > 2048:
            raise ValueError("search query must contain 1-2048 characters")
        params = copy.deepcopy(profile["params"])
        params[profile["query_field"]] = query
        if profile["limit_field"]:
            params[profile["limit_field"]] = max(1, min(20, int(limit)))
        data = _json_request(profile["endpoint"], profile["method"], params, _headers(profile))
        items = field(data, profile["results_path"])
        if not isinstance(items, list):
            raise ValueError("external search response has no result list at the configured path")
        results, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            url = field(item, profile["url_field"])
            if not public_link(url) or url in seen:
                continue
            seen.add(url)
            results.append({"url": url, "title": clean_text(field(item, profile["title_field"]), 1000),
                            "snippet": clean_text(field(item, profile["snippet_field"]), 1000)})
            if len(results) >= limit:
                break
        return {"ok": True, "engine": "external/" + provider, "results": results, "error": ""}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        error = "external search request failed (HTTP %s)" % status
    except ValueError as exc:
        # JSON parsing errors may contain remote content. Configuration errors
        # above are safe, but use a fixed message for malformed JSON responses.
        error = "external search returned invalid JSON" if isinstance(exc, json.JSONDecodeError) else str(exc)
    except Exception:
        error = "external search request failed (network or response error)"
    return {"ok": False, "engine": "external/" + provider, "results": [], "error": error}


def fetch(profile, url):
    from qoder_webtools import MAX_FETCH_CHARS, check_url

    result = {"ok": False, "engine": "external", "url": url}
    try:
        profile = normalize_fetch(profile, ready=True)
        ok, reason = check_url(url)
        if not ok:
            return dict(result, error=reason)
        params = dict(profile["params"])
        params[profile["url_field"]] = url
        data = _json_request(profile["endpoint"], profile["method"], params, _headers(profile))
        text = field(data, profile["text_path"])
        if not isinstance(text, str) or not text.strip():
            return dict(result, error="external fetch response has no text at the configured path")
        # The configured fetcher is trusted to enforce public-only egress at
        # connection time on every hop. This checks its report, not its transport.
        final_url = field(data, profile["final_url_path"])
        if not public_link(final_url) or not check_url(final_url)[0]:
            return dict(result, error="external fetch response has an invalid final URL")
        title = clean_text(field(data, profile["title_path"]), 1000) if profile["title_path"] else ""
        return dict(result, ok=True, final_url=final_url, title=title,
                    text=text[:MAX_FETCH_CHARS], truncated=len(text) > MAX_FETCH_CHARS, error="")
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return dict(result, error="external fetch request failed (HTTP %s)" % status)
    except json.JSONDecodeError:
        return dict(result, error="external fetch returned invalid JSON")
    except Exception:
        return dict(result, error="external fetch request failed (network or response error)")
