#!/usr/bin/env python3
"""Standalone MCP server (stdio, JSON-RPC 2.0, NDJSON) named `web`.

Exposes simple functions with typed args: web search (keyless DuckDuckGo
reimplementation), web fetch via Jina Reader, browser automation via
playwright/chromium, yt-dlp downloads, aria2 downloads, plus tool
status/install helpers. Missing binaries/packages are LAZY AUTO-INSTALLED
on first use; MCP startup never blocks on installs.

Protocol conventions (initialize, notifications/*, tools/list, tools/call,
error format, text content blocks) mirror memory_mcp.py so both servers
behave identically.
"""
import atexit
import glob
import html as htmlmod
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_INSTALL_LOCK = threading.Lock()
_INSTALLING = set()

_BROWSER = None
_PAGE = None
_PW = None


def log_err(msg):
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _tail(text, n=1500):
    if not isinstance(text, str):
        text = str(text)
    return text[-n:]


def _run(cmd, timeout):
    """Run cmd, return (returncode, combined_output)."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            errors="replace",
        )
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = ""
        try:
            if e.stdout:
                out += e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else str(e.stdout)
            if e.stderr:
                out += e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else str(e.stderr)
        except Exception:
            pass
        return 124, out + ("\n[TIMEOUT after %ss]" % timeout)
    except Exception as e:
        return 127, "failed to exec %s: %s" % (" ".join(cmd), e)


# ---------------- status helpers ----------------

def _playwright_status():
    spec = importlib.util.find_spec("playwright")
    if spec is None:
        return False, "python package 'playwright' not importable"
    try:
        import playwright  # noqa: F401
        return True, "python package 'playwright' importable"
    except Exception as e:
        return False, "playwright found but import failed: %s" % e


def _cache_homes():
    homes = []
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        homes.append(os.path.join(xdg, "ms-playwright"))
    bp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if bp:
        homes.append(bp)
    homes.append(os.path.expanduser("~/.cache/ms-playwright"))
    seen = []
    for h in homes:
        if h and h not in seen:
            seen.append(h)
    return seen


def _chromium_status():
    roots = _cache_homes() + ["/root/.cache/ms-playwright", "/ms-playwright"]
    pats = ("chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-linux64/chrome",
            "chromium-*/chrome-linux/headless_shell",
            "chromium_headless_shell-*/chrome-linux/headless_shell",
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
            "chrome-linux/chrome",
            "chrome-linux64/chrome")
    found = []
    for home in roots:
        for pat in pats:
            try:
                found.extend(g for g in glob.glob(os.path.join(home, pat)) if os.path.isfile(g))
            except Exception:
                pass
    if found:
        return True, "chromium executable present: %s" % found[0]
    # Ground truth: ask playwright where it expects the binary.
    if importlib.util.find_spec("playwright") is not None:
        try:
            rc, out = _run([sys.executable, "-c",
                            "from playwright.sync_api import sync_playwright as _s;"
                            " _p=_s().start(); print(_p.chromium.executable_path); _p.stop()"], 60)
            if rc == 0:
                cand = out.strip().splitlines()[-1].strip()
                if cand and os.path.isfile(cand):
                    return True, "chromium executable present: %s" % cand
                # Headless-shell sibling under the same browsers root.
                root = cand
                for _ in range(3):
                    root = os.path.dirname(root)
                for rel in ("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                            "chromium_headless_shell-*/chrome-linux/headless_shell"):
                    hits = [g for g in glob.glob(os.path.join(root, rel)) if os.path.isfile(g)]
                    if hits:
                        return True, "chromium executable present: %s" % hits[0]
        except Exception:
            pass
        return False, "no chromium executable found (checked XDG/HOME cache roots and playwright registry)"
    return False, "no chromium executable found (playwright pkg missing)"
    # Fallback: ask playwright for a dry run (only if package installed).
    if importlib.util.find_spec("playwright") is not None:
        rc, out = _run([sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"], 60)
        if rc == 0 and ("is already installed" in out or "chromium" in out.lower()):
            if "already installed" in out.lower() or "up to date" in out.lower():
                return True, "playwright reports chromium installed (dry-run)"
        return False, "no chromium executable under ~/.cache/ms-playwright"
    return False, "no chromium executable under ~/.cache/ms-playwright (playwright pkg missing)"


def _yt_dlp_status():
    cli = shutil.which("yt-dlp")
    mod = importlib.util.find_spec("yt_dlp")
    if cli:
        return True, "CLI found: %s" % cli
    if mod is not None:
        return True, "python module 'yt_dlp' importable (no CLI on PATH)"
    return False, "neither 'yt-dlp' CLI nor python module 'yt_dlp' found"


def _aria2_status():
    cli = shutil.which("aria2c")
    if cli:
        return True, "CLI found: %s" % cli
    return False, "'aria2c' not found on PATH"


def do_tools_status(args):
    return {
        "playwright": dict(zip(("installed", "detail"), _playwright_status())),
        "chromium": dict(zip(("installed", "detail"), _chromium_status())),
        "yt_dlp": dict(zip(("installed", "detail"), _yt_dlp_status())),
        "aria2c": dict(zip(("installed", "detail"), _aria2_status())),
    }


# ---------------- install helpers ----------------

def _pip_install(pkg):
    return _run([sys.executable, "-m", "pip", "install", "--quiet", pkg], 600)


def _install_playwright():
    logs = []
    rc, out = _pip_install("playwright")
    logs.append("$ %s -m pip install playwright\n%s" % (sys.executable, out))
    if rc != 0:
        return False, _tail("\n".join(logs))
    rc, out = _run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], 600)
    logs.append("$ playwright install --with-deps chromium\n%s" % out)
    if rc != 0:
        logs.append("NOTE: --with-deps failed (often apt issues); retrying without --with-deps")
        rc2, out2 = _run([sys.executable, "-m", "playwright", "install", "chromium"], 600)
        logs.append("$ playwright install chromium\n%s" % out2)
        rc = rc2
    if rc != 0:
        return False, _tail("\n".join(logs))
    ok_pw, _ = _playwright_status()
    ok_ch, _ = _chromium_status()
    if ok_pw and ok_ch:
        return True, _tail("\n".join(logs))
    logs.append("post-install check: playwright=%s chromium=%s" % (ok_pw, ok_ch))
    return (ok_pw and ok_ch), _tail("\n".join(logs))


def _install_yt_dlp():
    rc, out = _pip_install("yt-dlp")
    log = "$ %s -m pip install yt-dlp\n%s" % (sys.executable, out)
    if rc != 0:
        return False, _tail(log)
    ok, _ = _yt_dlp_status()
    if not ok:
        log += "\npost-install check failed"
    return ok, _tail(log)


def _install_aria2():
    logs = []
    if shutil.which("apt-get") and os.geteuid() == 0:
        rc, out = _run(["apt-get", "update", "-qq"], 300)
        logs.append("$ apt-get update -qq\n%s" % out)
        rc2, out2 = _run(["apt-get", "install", "-y", "-q", "aria2"], 600)
        logs.append("$ apt-get install -y -q aria2\n%s" % out2)
        if rc2 == 0 and shutil.which("aria2c"):
            return True, _tail("\n".join(logs))
        logs.append("apt path failed (rc=%s); trying apk/brew fallbacks" % rc2)
    elif shutil.which("apt-get"):
        logs.append("apt-get exists but not running as root (uid=%s); skipping apt" % os.geteuid())
    if shutil.which("apk"):
        rc, out = _run(["apk", "add", "aria2"], 600)
        logs.append("$ apk add aria2\n%s" % out)
        if rc == 0 and shutil.which("aria2c"):
            return True, _tail("\n".join(logs))
    if shutil.which("brew"):
        rc, out = _run(["brew", "install", "aria2"], 600)
        logs.append("$ brew install aria2\n%s" % out)
        if rc == 0 and shutil.which("aria2c"):
            return True, _tail("\n".join(logs))
    logs.append("aria2 install FAILED: no working provider (need apt-get as root, apk, or brew)")
    return False, _tail("\n".join(logs))


_INSTALLERS = {
    "playwright": _install_playwright,
    "yt-dlp": _install_yt_dlp,
    "aria2": _install_aria2,
}


def _install_one(name):
    with _INSTALL_LOCK:
        if name in _INSTALLING:
            return {"installed": False, "log_tail": "install of %r already in progress; retry shortly" % name}
        _INSTALLING.add(name)
    try:
        try:
            ok, tail = _INSTALLERS[name]()
        except Exception as e:
            ok, tail = False, "installer raised: %s" % e
        return {"installed": bool(ok), "log_tail": _tail(tail)}
    finally:
        with _INSTALL_LOCK:
            _INSTALLING.discard(name)


def do_tools_install(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "name" not in args:
        raise ValueError("missing required argument: name")
    name = args["name"]
    if name not in ("all", "playwright", "yt-dlp", "aria2"):
        raise ValueError("name must be one of all, playwright, yt-dlp, aria2")
    targets = ["playwright", "yt-dlp", "aria2"] if name == "all" else [name]
    return {t: _install_one(t) for t in targets}


def _ensure(names):
    """Ensure named tools installed; return installed_now flag."""
    installed_now = False
    for n in names:
        checker = {"playwright": _playwright_status, "yt-dlp": _yt_dlp_status, "aria2": _aria2_status}[n]
        ok, _ = checker()
        if not ok:
            res = _install_one(n)
            if res.get("installed"):
                installed_now = True
    return installed_now


# ---------------- search ----------------

class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._cap = None  # (kind, href)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._cap = ("title", d.get("href", ""))
            self._buf = []
        elif tag == "a" and "result__snippet" in cls:
            self._cap = ("snippet", "")
            self._buf = []
        elif tag == "td" and "result-snippet" in cls:
            self._cap = ("snippet", "")
            self._buf = []

    def handle_data(self, data):
        if self._cap:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if not self._cap:
            return
        kind, href = self._cap
        if (tag == "a" and kind in ("title", "snippet")) or (tag == "td" and kind == "snippet"):
            text = htmlmod.unescape(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            if kind == "title":
                self.results.append({"title": text, "url": href, "snippet": ""})
            elif kind == "snippet" and self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = text[:300]
            self._cap = None
            self._buf = []


def _resolve_ddg_url(href):
    if not href:
        return ""
    try:
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                return urllib.parse.unquote(m.group(1))
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return "https://duckduckgo.com" + href
        return href
    except Exception:
        return href


def _ddg_fetch(endpoint, query, post=False):
    data = None
    url = endpoint + urllib.parse.urlencode({"q": query})
    headers = {"User-Agent": UA, "Accept": "text/html"}
    if post:
        url = endpoint
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


def do_search(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "query" not in args:
        raise ValueError("missing required argument: query")
    query = args["query"]
    num = args.get("num_results", 8)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(num, int) or isinstance(num, bool):
        raise ValueError("num_results must be an int")
    num = max(1, min(num, 20))
    html_text = ""
    try:
        html_text = _ddg_fetch("https://html.duckduckgo.com/html/?", query, post=True)
    except Exception as e1:
        try:
            html_text = _ddg_fetch("https://lite.duckduckgo.com/lite/?", query, post=True)
        except Exception as e2:
            return {"error": "search failed", "detail": "html endpoint: %s; lite fallback: %s" % (e1, e2)}
    parser = _DDGParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    out = []
    for r in parser.results:
        url = _resolve_ddg_url(r.get("url", ""))
        if not url or url.startswith("https://duckduckgo.com/y.js"):
            continue
        out.append({"title": r.get("title", ""), "url": url, "snippet": (r.get("snippet", "") or "")[:300]})
        if len(out) >= num:
            break
    return {
        "engine": "duckduckgo-html (keyless reimplementation of opencode builtin websearch — MCP cannot call the builtin)",
        "results": out,
    }


# ---------------- fetch ----------------

def _html_to_text(html_text):
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1\s*>", " ", html_text)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = htmlmod.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def do_fetch(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    fmt = args.get("format", "markdown")
    max_chars = args.get("max_chars", 20000)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if fmt not in ("markdown", "text"):
        raise ValueError('format must be one of markdown, text')
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise ValueError("max_chars must be an int")
    max_chars = max(1, min(max_chars, 200000))
    jina_url = "https://r.jina.ai/" + url
    headers = {
        # jina-reader 403s browser-mimicking UAs; non-browser UA returns 200 (verified)
        "User-Agent": "python-requests/2.32.3",
        "Accept": "text/plain",
        "X-Return-Format": "markdown" if fmt == "markdown" else "text",
    }
    try:
        req = urllib.request.Request(jina_url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError("jina-reader HTTP %s" % r.status)
            raw = r.read().decode("utf-8", "replace")
        truncated = len(raw) > max_chars
        return {"url": url, "source": "jina-reader", "content": raw[:max_chars], "truncated": truncated}
    except Exception as e1:
        reason = str(e1) or repr(e1)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
            text = _html_to_text(raw)
            truncated = len(text) > max_chars
            return {
                "url": url,
                "source": "direct-fallback (%s)" % reason,
                "content": text[:max_chars],
                "truncated": truncated,
            }
        except Exception as e2:
            return {"error": "fetch failed", "detail": "jina-reader: %s; direct fallback: %s" % (reason, e2)}


# ---------------- browser ----------------

def _ensure_browser():
    """Return (installed_now_bool). Launches singleton browser via sync API."""
    global _BROWSER, _PAGE, _PW
    installed_now = _ensure(["playwright"])
    ok_ch, _ = _chromium_status()
    if not ok_ch:
        res = _install_one("playwright")
        if res.get("installed"):
            installed_now = True
        else:
            raise RuntimeError("chromium install failed: %s" % res.get("log_tail", "")[-500:])
    if _BROWSER is not None:
        try:
            if _PAGE is not None:
                return installed_now
        except Exception:
            pass
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError("playwright import failed even after ensure: %s" % e)
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], headless=True)
    except Exception as e:
        try:
            pw.stop()
        except Exception:
            pass
        raise RuntimeError("chromium launch failed: %s" % e)
    page = browser.new_page()
    page.set_default_timeout(30000)
    _PW = pw
    _BROWSER = browser
    _PAGE = page
    return installed_now


def _close_browser():
    global _BROWSER, _PAGE, _PW
    try:
        if _BROWSER is not None:
            _BROWSER.close()
    except Exception:
        pass
    try:
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass
    _BROWSER = None
    _PAGE = None
    _PW = None


atexit.register(_close_browser)


def do_browser_open(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    wait_ms = args.get("wait_ms", 1500)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool):
        raise ValueError("wait_ms must be an int")
    wait_ms = max(0, min(wait_ms, 30000))
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.goto(url, wait_until="load", timeout=30000)
        _PAGE.wait_for_timeout(wait_ms)
        title = _PAGE.title()
        try:
            text = _PAGE.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        out = {"url": _PAGE.url, "title": title, "text": text[:50000]}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_open failed", "detail": str(e)}


def do_browser_click(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    target = args["text"]
    if not isinstance(target, str) or not target:
        raise ValueError("text must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if target[0] in (".", "#", ">"):
            _PAGE.click(target, timeout=30000)
            clicked = target
        else:
            _PAGE.get_by_text(target).first.click(timeout=30000)
            clicked = target
        _PAGE.wait_for_timeout(1000)
        try:
            cur = _PAGE.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            cur = ""
        out = {"ok": True, "text": (cur[:50000] if isinstance(cur, str) else str(cur)[:50000])}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_click failed", "detail": "could not click %r: %s" % (target, e)}


def do_browser_screenshot(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "path" not in args:
        raise ValueError("missing required argument: path")
    path = args["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        _PAGE.screenshot(path=path)
        size = os.path.getsize(path)
        out = {"path": path, "bytes": size}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_screenshot failed", "detail": str(e)}


def do_browser_extract(args):
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    selector = args.get("selector", "body")
    if not isinstance(selector, str) or not selector:
        raise ValueError("selector must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        text = _PAGE.inner_text(selector, timeout=30000)
        out = {"text": (text[:50000] if isinstance(text, str) else str(text)[:50000])}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_extract failed", "detail": str(e)}


# ---------------- yt-dlp ----------------

def _yt_cmd():
    cli = shutil.which("yt-dlp")
    if cli:
        return [cli], cli
    return [sys.executable, "-m", "yt_dlp"], "python -m yt_dlp"


def _tool_version(cmd, flag="--version"):
    try:
        rc, out = _run(cmd + [flag], 60)
        if rc == 0:
            return out.strip().splitlines()[0][:100] if out.strip() else "unknown"
        return "unknown"
    except Exception:
        return "unknown"


def do_yt_download(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    output_dir = args.get("output_dir", ".")
    audio_only = args.get("audio_only", False)
    filename = args.get("filename")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output_dir must be a non-empty string")
    if not isinstance(audio_only, bool):
        raise ValueError("audio_only must be a boolean")
    if filename is not None and not isinstance(filename, str):
        raise ValueError("filename must be a string or null")
    installed_now = _ensure(["yt-dlp"])
    base, tool_label = _yt_cmd()
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        return {"error": "yt_download failed", "detail": "cannot create output_dir: %s" % e,
                "tool": "yt-dlp", "installed_now": installed_now,
                "tool_version": _tool_version(base)}
    template = os.path.join(output_dir, filename if filename else "%(title)s.%(ext)s")
    cmd = base + ["-o", template, "--no-progress", "--newline",
                  "--print", "after_move:filepath", url]
    if audio_only:
        cmd = base + ["-o", template, "--extract-audio", "--audio-format", "mp3",
                      "--no-progress", "--newline", "--print", "after_move:filepath", url]
    rc, out = _run(cmd, 300)
    version = _tool_version(base)
    if rc != 0:
        return {"error": "yt_download failed", "detail": _tail(out, 2000),
                "tool": "yt-dlp", "installed_now": installed_now, "tool_version": version}
    files = []
    for line in out.splitlines():
        line = line.strip()
        if line and os.path.isfile(line):
            try:
                files.append({"path": line, "bytes": os.path.getsize(line)})
            except Exception:
                pass
    if not files:
        # Fallback: newest files in output_dir (yt-dlp may print nothing on some versions).
        try:
            cands = [os.path.join(output_dir, f) for f in os.listdir(output_dir)]
            cands = [p for p in cands if os.path.isfile(p)]
            cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for p in cands[:5]:
                files.append({"path": p, "bytes": os.path.getsize(p)})
        except Exception:
            pass
    if not files:
        return {"error": "yt_download reported success but no output file found",
                "detail": _tail(out, 2000), "tool": "yt-dlp",
                "installed_now": installed_now, "tool_version": version}
    return {"files": files, "tool": "yt-dlp", "installed_now": installed_now, "tool_version": version}


# ---------------- aria2 ----------------

def do_aria2_download(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    output_dir = args.get("output_dir", ".")
    filename = args.get("filename")
    connections = args.get("connections", 4)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output_dir must be a non-empty string")
    if filename is not None and not isinstance(filename, str):
        raise ValueError("filename must be a string or null")
    if not isinstance(connections, int) or isinstance(connections, bool):
        raise ValueError("connections must be an int")
    connections = max(1, min(connections, 16))
    installed_now = _ensure(["aria2"])
    if not shutil.which("aria2c"):
        return {"error": "aria2c unavailable", "detail": "aria2c not installed and auto-install failed; run tools_install('aria2') for the log",
                "tool": "aria2c", "installed_now": installed_now, "tool_version": "unknown"}
    version = _tool_version(["aria2c"])
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        return {"error": "aria2_download failed", "detail": "cannot create output_dir: %s" % e,
                "tool": "aria2c", "installed_now": installed_now, "tool_version": version}
    before = set()
    try:
        before = set(os.listdir(output_dir))
    except Exception:
        pass
    cmd = ["aria2c", "-x", str(connections), "-s", str(connections),
           "-d", output_dir, "--console-log-level=warn", "--summary-interval=0"]
    if filename:
        cmd += ["-o", filename]
    cmd += [url]
    rc, out = _run(cmd, 300)
    if rc != 0:
        return {"error": "aria2_download failed", "detail": _tail(out, 2000),
                "tool": "aria2c", "installed_now": installed_now, "tool_version": version}
    files = []
    try:
        if filename:
            p = os.path.join(output_dir, filename)
            if os.path.isfile(p):
                files.append({"path": p, "bytes": os.path.getsize(p)})
        else:
            for f in sorted(set(os.listdir(output_dir)) - before):
                p = os.path.join(output_dir, f)
                if os.path.isfile(p):
                    files.append({"path": p, "bytes": os.path.getsize(p)})
            if not files:
                for f in sorted(os.listdir(output_dir)):
                    p = os.path.join(output_dir, f)
                    if os.path.isfile(p):
                        files.append({"path": p, "bytes": os.path.getsize(p)})
    except Exception as e:
        return {"error": "aria2_download listing failed", "detail": str(e),
                "tool": "aria2c", "installed_now": installed_now, "tool_version": version}
    if not files:
        return {"error": "aria2c reported success but no output file found",
                "detail": _tail(out, 2000), "tool": "aria2c",
                "installed_now": installed_now, "tool_version": version}
    return {"files": files, "tool": "aria2c", "installed_now": installed_now, "tool_version": version}


# ---------------- MCP wiring (mirrors memory_mcp.py) ----------------

TOOLS = [
    {
        "name": "tools_status",
        "description": "Report whether playwright, chromium, yt-dlp and aria2c are installed, with details.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "tools_install",
        "description": "Install a missing helper tool (playwright, yt-dlp, aria2, or all) and return per-item status with log tails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": ["all", "playwright", "yt-dlp", "aria2"]},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search",
        "description": "Search the web via keyless DuckDuckGo HTML endpoints and return titles, urls and snippets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch",
        "description": "Read any web page via Jina Reader (r.jina.ai) as clean markdown; falls back to direct fetch and says which source was used.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "format": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_open",
        "description": "Open a URL in headless Chromium (auto-installs playwright on first use) and return the page title and text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wait_ms": {"type": "integer", "default": 1500},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click a page element by its visible text (or a CSS selector starting with . # or >) in the open headless page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Save a screenshot of the open headless page to a file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_extract",
        "description": "Extract the visible text of a CSS selector (default body) from the open headless page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": "body"},
            },
        },
    },
    {
        "name": "yt_download",
        "description": "Download media with yt-dlp (auto-installed on first use); supports audio-only mp3 extraction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_dir": {"type": "string", "default": "."},
                "audio_only": {"type": "boolean", "default": False},
                "filename": {"type": ["string", "null"], "default": None},
            },
            "required": ["url"],
        },
    },
    {
        "name": "aria2_download",
        "description": "Download a file with aria2c using multiple connections (auto-installed on first use).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_dir": {"type": "string", "default": "."},
                "filename": {"type": ["string", "null"], "default": None},
                "connections": {"type": "integer", "default": 4},
            },
            "required": ["url"],
        },
    },
]

HANDLERS = {
    "tools_status": do_tools_status,
    "tools_install": do_tools_install,
    "search": do_search,
    "fetch": do_fetch,
    "browser_open": do_browser_open,
    "browser_click": do_browser_click,
    "browser_screenshot": do_browser_screenshot,
    "browser_extract": do_browser_extract,
    "yt_download": do_yt_download,
    "aria2_download": do_aria2_download,
}


def handle_message(msg):
    method = msg.get("method") if isinstance(msg, dict) else None
    has_id = isinstance(msg, dict) and "id" in msg
    req_id = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(msg, dict) or not isinstance(method, str):
        if has_id:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "Invalid Request"}}
        return None
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        pv = params.get("protocolVersion") if isinstance(params, dict) else None
        if not isinstance(pv, str):
            pv = "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "web", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if not isinstance(params, dict):
            params = {}
        name = params.get("name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        try:
            if name not in HANDLERS:
                raise ValueError("unknown tool: %r" % (name,))
            result = HANDLERS[name](arguments)
            if isinstance(result, (dict, list)):
                text = json.dumps(result)
            elif not isinstance(result, str):
                text = str(result)
            else:
                text = result
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            try:
                emsg = str(e) if str(e) else repr(e)
            except Exception:
                emsg = "error"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "Error: " + emsg}], "isError": True},
            }
    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}


def main():
    stdin = sys.stdin
    stdout = sys.stdout
    while True:
        line = stdin.readline()
        if line == "":
            break
        if line.strip() == "":
            continue
        try:
            msg = json.loads(line)
        except Exception:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:
            try:
                rid = msg.get("id") if isinstance(msg, dict) and "id" in msg else None
            except Exception:
                rid = None
            if rid is None and not (isinstance(msg, dict) and "id" in msg):
                continue
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error: " + str(e)}}
        if resp is None:
            continue
        stdout.write(json.dumps(resp) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
