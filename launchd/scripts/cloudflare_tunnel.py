#!/usr/bin/env python3
"""Cloudflare Tunnel for employee web portal.

Supports two modes, selected by available credentials:

Named Tunnel (recommended — fixed URL, no Google Console update needed):
  Token  — read from macOS keyring (service=xiaohong-agent,
           user=cloudflare-tunnel-token); env fallback CLOUDFLARE_TUNNEL_TOKEN.
  Hostname — env CLOUDFLARE_TUNNEL_HOSTNAME, e.g. portal.company.example
  Command: cloudflared tunnel run --token <TOKEN>

Quick Tunnel (fallback — URL rotates on every restart):
  Neither token nor hostname available.
  Command: cloudflared tunnel --url http://localhost:8080 --no-autoupdate
  URL captured from cloudflared output and pushed to 大王 via Telegram.

In both modes the resolved public URL is saved to var/data/tunnel_url.txt
so that app.py/_get_oauth_redirect_base() picks it up automatically.

Why keyring for the token: the plist lives in git. Mirrors the telegram.py /
gemini_client.py pattern — secrets in keyring, non-secret config in plist.

Triggered by: launchd com.xiaohong.cloudflare_tunnel (KeepAlive)
"""
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_core.daemon_helpers import rotate_log
from agent_core.logging_and_paths import DATA_DIR

try:
    import keyring
except ImportError:
    keyring = None

_KEYRING_SERVICE = "xiaohong-agent"
_KEYRING_TOKEN_USER = "cloudflare-tunnel-token"


def _get_tunnel_token() -> str:
    """Resolve the Cloudflare Named Tunnel token.

    Order:
      1. macOS keyring (service=xiaohong-agent, user=cloudflare-tunnel-token)
      2. CLOUDFLARE_TUNNEL_TOKEN env var (escape hatch / non-mac dev)
    """
    if keyring is not None:
        try:
            val = (keyring.get_password(_KEYRING_SERVICE, _KEYRING_TOKEN_USER) or "").strip()
            if val:
                return val
        except Exception as exc:
            print(f"[tunnel] keyring 讀取 token 失敗：{exc}", file=sys.stderr)
    return os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip()

_URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
# cloudflared readiness signals vary across versions:
#   new (2022+):  "Registered tunnel connection"  (JSON structured logs)
#   legacy:       "Connection ... registered with protocol"
#   intermediate: "Connection registered"
# Matching all variants ensures older cloudflared builds still trigger the
# URL save + Telegram notification.
# (?<!un) negative lookbehind prevents matching shutdown lines such as
# "Unregistered tunnel connection" which contain "registered tunnel connection"
# as a substring and would otherwise trigger a false readiness signal.
_NAMED_READY_RE = re.compile(
    r"(?<!un)registered tunnel connection|registered with protocol|Connection registered",
    re.IGNORECASE,
)
# Use DATA_DIR (honouring RED_RUNTIME_DIR) so app.py and the tunnel script
# always read/write the same file, even in non-default runtime directories.
_URL_FILE = Path(DATA_DIR) / "tunnel_url.txt"


def _notify_telegram(url: str) -> None:
    try:
        from agent_core.telegram import telegram_push
        msg = (
            f"🌐 員工入口已上線\n"
            f"URL：{url}\n\n"
            f"（請轉發給員工）"
        )
        telegram_push(msg)
        print(f"[tunnel] Telegram 通知已送出：{url}")
    except Exception as exc:
        print(f"[tunnel] Telegram 通知失敗：{exc}", file=sys.stderr)


def _save_url(url: str) -> None:
    try:
        _URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _URL_FILE.write_text(url, encoding="utf-8")
        print(f"[tunnel] URL 已寫入 {_URL_FILE}")
    except Exception as exc:
        print(f"[tunnel] URL 寫檔失敗：{exc}", file=sys.stderr)


def _watch_stream(stream, notified_flag: list) -> None:
    """Scan one output stream (stdout or stderr) for the quick-tunnel URL."""
    for line in stream:
        line = line.rstrip()
        print(f"[cloudflared] {line}", flush=True)
        if not notified_flag[0]:
            m = _URL_RE.search(line)
            if m:
                url = m.group(0)
                notified_flag[0] = True
                print(f"[tunnel] Quick Tunnel URL 取得：{url}")
                _save_url(url)
                threading.Thread(target=_notify_telegram, args=(url,), daemon=True).start()


def _watch_named_stream(stream, public_url: str, notified_flag: list) -> None:
    """Log named-tunnel output; save URL + notify once cloudflared confirms a connection."""
    for line in stream:
        line = line.rstrip()
        print(f"[cloudflared] {line}", flush=True)
        if not notified_flag[0] and _NAMED_READY_RE.search(line):
            notified_flag[0] = True
            print(f"[tunnel] Named Tunnel 已確認上線：{public_url}")
            _save_url(public_url)
            threading.Thread(target=_notify_telegram, args=(public_url,), daemon=True).start()


def _run_named_tunnel(token: str, hostname: str) -> None:
    """Run a Cloudflare Named Tunnel using a pre-issued token.

    URL-file strategy (three cases):

      1. Same hostname as stored URL (KeepAlive restart after a successful run):
         Keep the existing file so app.py continues serving valid OAuth redirects
         during this transient restart.  _watch_named_stream overwrites on success.

      2. Different hostname / Quick-Tunnel URL / no prior file:
         The stored URL is wrong for this tunnel.  Write only after cloudflared
         confirms the connection.  If startup fails, remove any stale file so
         app.py falls back to localhost (clear error) rather than redirecting
         OAuth to a dead or unrelated domain.
    """
    public_url = f"https://{hostname.lstrip('/')}"

    try:
        stored_url = _URL_FILE.read_text(encoding="utf-8").strip() if _URL_FILE.exists() else ""
    except Exception:
        stored_url = ""

    same_host = stored_url == public_url

    if same_host:
        print(f"[tunnel] Named Tunnel 模式 → {public_url}（保留既有 URL，等待確認上線）")
    elif stored_url:
        print(
            f"[tunnel] Named Tunnel 模式 → {public_url}"
            f"（hostname 已變更，舊 URL={stored_url}，確認後更新）"
        )
    else:
        print(f"[tunnel] Named Tunnel 模式 → {public_url}（首次啟動，等待確認後寫入 URL）")

    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "run", "--token", token],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    # Watch both stdout and stderr for the readiness signal.
    # A single notified_flag is shared so the URL write + Telegram notification
    # fire at most once regardless of which stream sees the signal first.
    notified_flag = [False]
    watchers = []
    for stream in (proc.stdout, proc.stderr):
        t = threading.Thread(
            target=_watch_named_stream,
            args=(stream, public_url, notified_flag),
            daemon=True,
        )
        t.start()
        watchers.append(t)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    # Drain any buffered output before checking notified_flag.  cloudflared
    # may log the readiness line just before exiting; without this join the
    # check below can race against watcher threads still reading the pipe.
    for t in watchers:
        t.join(timeout=5)
    if not notified_flag[0]:
        print(
            "[tunnel] ⚠️  cloudflared 結束前未確認上線（token 無效或網路問題）",
            file=sys.stderr,
        )
        if not same_host:
            # Stale or mismatched URL in the file — clear it so app.py does not
            # keep routing OAuth callbacks to a wrong or unreachable domain.
            try:
                if _URL_FILE.exists():
                    _URL_FILE.unlink()
                    print(f"[tunnel] 已清除舊 URL（{stored_url or '無'}）", file=sys.stderr)
            except Exception as exc:
                print(f"[tunnel] 清除舊 URL 失敗：{exc}", file=sys.stderr)


def _run_quick_tunnel() -> None:
    """Run a Cloudflare Quick Tunnel and capture the rotating URL."""
    print("[tunnel] Quick Tunnel 模式 → http://localhost:8080")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://localhost:8080", "--no-autoupdate"],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    # cloudflared may write the URL to stdout or stderr depending on version.
    notified_flag = [False]
    stdout_watcher = threading.Thread(
        target=_watch_stream, args=(proc.stdout, notified_flag), daemon=True
    )
    stderr_watcher = threading.Thread(
        target=_watch_stream, args=(proc.stderr, notified_flag), daemon=True
    )
    stdout_watcher.start()
    stderr_watcher.start()
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


def main():
    rotate_log("cloudflare_tunnel")

    token = _get_tunnel_token()
    hostname = os.environ.get("CLOUDFLARE_TUNNEL_HOSTNAME", "").strip()

    if token and hostname:
        _run_named_tunnel(token, hostname)
    elif token and not hostname:
        print(
            "[tunnel] ⚠️  CLOUDFLARE_TUNNEL_TOKEN 已設定，但缺少 CLOUDFLARE_TUNNEL_HOSTNAME。"
            " 改用 Quick Tunnel。",
            file=sys.stderr,
        )
        _run_quick_tunnel()
    else:
        _run_quick_tunnel()

    print("[tunnel] cloudflared 結束，launchd 將自動重啟。")


if __name__ == "__main__":
    main()
