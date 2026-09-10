"""HTML view of the dashboard — generates a single-file HTML page with
inline SVG cost chart + colored daemon table + alert banner.

兩種使用方式：
  - `bin/red-web` 一次性產生 HTML 到 /tmp/red-status.html 並用瀏覽器開
  - `serve_dashboard(port=8765)` 跑 localhost http server，瀏覽器可
    auto-refresh 每 30 秒（read-only，僅 bind 127.0.0.1）

無 third-party deps — 用 stdlib http.server + 自製 inline SVG。
適合「想要視覺化但不想裝 Flask + Chart.js」的情境。
"""
from __future__ import annotations

import html
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time
import webbrowser
from datetime import date, datetime
from typing import Optional


# ────────────────────────────────────────────────────────────────────
# SVG sparkline / bar chart — 不用 third-party lib
# ────────────────────────────────────────────────────────────────────
def _svg_cost_chart(daily_usd: dict[str, float], width: int = 600, height: int = 100) -> str:
    """畫過去 14 天 cost 直條圖，用 inline SVG。

    daily_usd 是 dict[date_str, usd]。空 → 回 placeholder。
    """
    if not daily_usd:
        return '<div class="chart-empty">（沒有 cost 資料）</div>'

    items = sorted(daily_usd.items())[-14:]
    if not items:
        return '<div class="chart-empty">（cost 資料太少）</div>'

    max_v = max(v for _, v in items) or 1
    bar_w = (width - 80) / len(items)
    today_str = date.today().isoformat()

    parts = [f'<svg class="cost-chart" width="{width}" height="{height + 30}" viewBox="0 0 {width} {height + 30}">']
    parts.append(f'<text x="0" y="14" fill="#888" font-size="11">過去 {len(items)} 天 USD</text>')
    parts.append(f'<text x="{width - 60}" y="14" fill="#888" font-size="11">max ${max_v:.3f}</text>')
    for i, (d, v) in enumerate(items):
        bar_h = max(2, (v / max_v) * height)
        x = 20 + i * bar_w
        y = height - bar_h + 20
        is_today = d == today_str
        color = "#e63946" if is_today else "#457b9d"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w * 0.8:.1f}" height="{bar_h:.1f}" '
            f'fill="{color}" rx="1"><title>{d}: ${v:.4f}</title></rect>'
        )
        # x-axis label every other day
        if i % 2 == 0 or is_today:
            parts.append(
                f'<text x="{x + bar_w * 0.4:.1f}" y="{height + 28}" '
                f'fill="#888" font-size="9" text-anchor="middle">{d[5:]}</text>'
            )
    parts.append('</svg>')
    return "".join(parts)


# ────────────────────────────────────────────────────────────────────
# HTML rendering
# ────────────────────────────────────────────────────────────────────
_CSS = """
body {
  font-family: -apple-system, "Helvetica Neue", "PingFang TC", sans-serif;
  background: #1d1d1f; color: #f5f5f7;
  margin: 0; padding: 20px; line-height: 1.5;
  font-size: 14px;
}
.container { max-width: 900px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 8px 0; }
h2 { font-size: 16px; margin: 18px 0 8px 0; padding-bottom: 4px;
     border-bottom: 1px solid #444; color: #98c1d9; }
.subtitle { color: #888; font-size: 12px; margin-bottom: 16px; }
.section { background: #2a2a2c; padding: 14px 18px; border-radius: 8px;
           margin: 12px 0; }
.section-pre { font-family: ui-monospace, SF Mono, Menlo, monospace;
               font-size: 12px; white-space: pre-wrap;
               word-break: break-all; color: #d0d0d0; margin: 0; }
.alert-banner { padding: 14px 18px; border-radius: 8px; margin: 12px 0;
                font-weight: 600; }
.alert-crit { background: #5a2020; border-left: 4px solid #e63946; }
.alert-warn { background: #5a4a20; border-left: 4px solid #ffd166; }
.alert-ok   { background: #20502d; border-left: 4px solid #06d6a0; }
.cost-chart { display: block; margin: 8px 0; }
.refresh { color: #98c1d9; text-decoration: none; font-size: 12px;
           background: #383838; padding: 4px 10px; border-radius: 4px; }
.refresh:hover { background: #4a4a4a; }
"""


def render_html(sections: str = "") -> str:
    """組整份 HTML（不含 server logic）。也給 bin/red-web 一次性產生用。"""
    from agent_core.dashboard import system_status
    from agent_core.dashboard_trends import cost_trend
    from agent_core.dashboard_alerts import check_alerts

    # Alert banner
    try:
        alerts = check_alerts()
    except Exception:
        alerts = []
    if alerts:
        crit = [a for a in alerts if a.get("level") == "crit"]
        warn = [a for a in alerts if a.get("level") == "warn"]
        banner_class = "alert-crit" if crit else "alert-warn"
        banner_lines = [f"🚨 {len(crit)} crit / {len(warn)} warn alert"]
        for a in alerts[:5]:
            banner_lines.append(f"  • [{a['level']}] {a['title']}：{a['detail']}")
        banner_html = (
            f'<div class="alert-banner {banner_class}">'
            + "<br>".join(html.escape(ln) for ln in banner_lines)
            + "</div>"
        )
    else:
        banner_html = '<div class="alert-banner alert-ok">✅ 目前無警示</div>'

    # Cost chart
    try:
        ct = cost_trend()
        chart_html = _svg_cost_chart(ct.get("daily_usd") or {})
    except Exception:
        chart_html = '<div class="chart-empty">（cost chart 失敗）</div>'

    # Main dashboard text
    try:
        body = system_status(sections)
    except Exception as e:
        body = f"system_status 失敗：{type(e).__name__}: {e}"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>RED status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_CSS}</style>
</head>
<body>
<div class="container">
  <h1>🖥️  RED 系統狀態</h1>
  <div class="subtitle">產生於 {now} <a class="refresh" href="?_={int(time.time())}">↻ 重新整理</a></div>
  {banner_html}
  <div class="section">
    <h2>過去 14 天 Gemini 成本</h2>
    {chart_html}
  </div>
  <div class="section">
    <pre class="section-pre">{html.escape(body)}</pre>
  </div>
  <div class="subtitle">
    📋 同份內容也可從 terminal：<code>./bin/red-status</code> 或
    LLM tool：<code>system_status()</code>
  </div>
</div>
</body>
</html>"""


# ────────────────────────────────────────────────────────────────────
# One-shot HTML generator (bin/red-web 用)
# ────────────────────────────────────────────────────────────────────
def generate_html_file(out_path: str = "/tmp/red-status.html",
                       sections: str = "") -> str:
    """產生 HTML 檔到 out_path，回傳檔案路徑。"""
    html_text = render_html(sections)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return out_path


def open_dashboard_in_browser(sections: str = "") -> str:
    """產生 HTML + 用系統預設瀏覽器開。回傳檔案路徑。"""
    path = generate_html_file(sections=sections)
    try:
        if os.uname().sysname == "Darwin":
            subprocess.run(["open", path], check=False, timeout=5)
        else:
            webbrowser.open(f"file://{path}")
    except Exception as e:
        return f"產生 OK 但開啟瀏覽器失敗：{e}\n手動開啟 {path}"
    return path


# ────────────────────────────────────────────────────────────────────
# Local HTTP server (live mode) — auto-refresh from browser
# ────────────────────────────────────────────────────────────────────
class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    """每 GET / 重新生成 HTML — 不快取，永遠是最新 system_status。"""

    def do_GET(self):  # noqa: N802 (stdlib API)
        try:
            html_text = render_html()
        except Exception as e:
            html_text = (f"<html><body><h1>render_html() 炸</h1>"
                         f"<pre>{html.escape(str(e))}</pre></body></html>")
        encoded = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        # 安靜，別洗 stdout — RedactFilter 也會過 logger.info 但這是 stdout 直寫
        pass


def serve_dashboard(port: int = 8765, open_browser: bool = True) -> str:
    """跑一個 localhost-only http server，每次刷新都重算 dashboard。

    這個函式會 **block** — 適合 CLI（`bin/red-web`）or `python -m`。
    LLM 別呼叫（沒意義，會卡死）。

    Args:
        port: 預設 8765。被佔用就試 +1。
        open_browser: True 時自動開瀏覽器。

    安全考量：
      - 只 bind 127.0.0.1（不對外開放）
      - 只接受 GET /，無寫入端點
      - 回傳的 HTML 已過 sanitize_for_llm（裡面 dashboard 文字本來就過了）
    """
    bind_addr = ("127.0.0.1", port)
    # 試最多 5 個 port，避免被佔用就直接放棄
    for _ in range(5):
        try:
            httpd = socketserver.TCPServer(bind_addr, _DashboardHandler)
            break
        except OSError:
            bind_addr = ("127.0.0.1", bind_addr[1] + 1)
    else:
        return "❌ 8765-8769 都被佔用，找不到 port"

    actual_port = bind_addr[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"[red-web] 🟢 listening on {url}")
    print("[red-web] Ctrl-C 結束")
    if open_browser:
        try:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[red-web] 收到 SIGINT，關閉")
        httpd.shutdown()
    return f"server 關閉（port {actual_port}）"
