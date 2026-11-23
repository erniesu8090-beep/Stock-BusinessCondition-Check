"""台股籌碼風險判讀儀表板本機 API 伺服器與 HTML 內嵌數據更新器。"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 確保可直接以 python src\server.py 執行
SRC_DIR = Path(__file__).resolve().parent
BASE_DIR = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from fetcher import (  # noqa: E402
    fetch_all_market_data,
    fetch_realtime_quotes,
    load_latest_market_data,
)

HTML_PATH = SRC_DIR / "code_artifact.html"
MASCOT_DIR = BASE_DIR / "assets" / "mascots"
ICON_PATH = BASE_DIR / "assets" / "app.ico"
EMBED_PATTERN = re.compile(
    r'(<script\s+id="embedded-market-data"\s+type="application/json">\s*)(.*?)(\s*</script>)',
    re.DOTALL,
)

_sync_lock = threading.Lock()
_sync_state: dict[str, Any] = {
    "syncing": False,
    "lastError": None,
    "lastFetchedAt": None,
    "lastFinishedMono": 0.0,
}

_realtime_lock = threading.Lock()
_realtime_cache: dict[str, Any] = {
    "fetchedMono": 0.0,
    "payload": None,
}


def get_realtime_quotes(force: bool = False, ttl_sec: float = 2.0) -> dict[str, Any]:
    """輕量取得大盤與 12 檔觀察名單即時股價，並於短時間內快取去抖。"""
    with _realtime_lock:
        if (
            not force
            and _realtime_cache["payload"] is not None
            and (time.monotonic() - float(_realtime_cache["fetchedMono"])) <= ttl_sec
        ):
            return _realtime_cache["payload"]

        cached = load_latest_market_data() or {}
        payload = fetch_realtime_quotes(cached_data=cached)
        _realtime_cache["payload"] = payload
        _realtime_cache["fetchedMono"] = time.monotonic()
        return payload


def embed_market_data_into_html(data: dict[str, Any]) -> bool:
    """將最新抓取的市場 JSON 內嵌回 src/code_artifact.html，支援雙擊離線開啟。"""
    if not HTML_PATH.exists():
        return False
    html_text = HTML_PATH.read_text(encoding="utf-8")
    if not EMBED_PATTERN.search(html_text):
        return False

    safe_json = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    updated_html = EMBED_PATTERN.sub(
        lambda m: f"{m.group(1)}{safe_json}{m.group(3)}", html_text, count=1
    )
    HTML_PATH.write_text(updated_html, encoding="utf-8")
    return True


def sync_and_embed(
    force_tdcc: bool = False, reuse_recent_sec: float = 0.0
) -> dict[str, Any]:
    """同步最新官方市場數據並回寫至 data JSON 與 code_artifact.html（具執行緒互斥鎖）。"""
    with _sync_lock:
        if (
            not force_tdcc
            and reuse_recent_sec > 0
            and (time.monotonic() - float(_sync_state["lastFinishedMono"])) <= reuse_recent_sec
        ):
            cached = load_latest_market_data()
            if cached is not None:
                return cached

        _sync_state["syncing"] = True
        _sync_state["lastError"] = None
        try:
            data = fetch_all_market_data(force_tdcc=force_tdcc)
            embed_market_data_into_html(data)
            _sync_state["lastFetchedAt"] = data.get("fetchedAt")
            _sync_state["lastFinishedMono"] = time.monotonic()
            return data
        except Exception as exc:
            _sync_state["lastError"] = str(exc)
            raise
        finally:
            _sync_state["syncing"] = False


class DashboardHandler(BaseHTTPRequestHandler):
    """提供靜態儀表板頁面與 /api/latest、/api/sync 介面。"""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """精簡伺服器終端日誌。"""
        print(f"[Server] {self.address_string()} - {format % args}", flush=True)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def _handle_sync_request(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        force_tdcc = query.get("force_tdcc", ["0"])[0].lower() in ("1", "true", "yes")
        try:
            data = sync_and_embed(
                force_tdcc=force_tdcc,
                reuse_recent_sec=0.0 if force_tdcc else 8.0,
            )
            self._send_json(HTTPStatus.OK, data)
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/code_artifact.html"):
            if not HTML_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到 code_artifact.html"})
                return
            content = HTML_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path in ("/favicon.ico", "/assets/app.ico"):
            if not ICON_PATH.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到圖示檔 app.ico"})
                return
            ico_bytes = ICON_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/x-icon")
            self.send_header("Cache-Control", "public, max-age=86400")
            self._send_cors_headers()
            self.send_header("Content-Length", str(len(ico_bytes)))
            self.end_headers()
            self.wfile.write(ico_bytes)
            return

        if path.startswith("/assets/mascots/") or path.startswith("/mascots/"):
            filename = Path(path).name
            if not filename.endswith(".webp") or ".." in path:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "不允許的檔案路徑"})
                return
            img_path = MASCOT_DIR / filename
            if not img_path.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"找不到圖檔：{filename}"})
                return
            img_bytes = img_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Cache-Control", "public, max-age=86400")
            self._send_cors_headers()
            self.send_header("Content-Length", str(len(img_bytes)))
            self.end_headers()
            self.wfile.write(img_bytes)
            return

        if path == "/api/health":
            cached = load_latest_market_data()
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "tw-whale-dashboard",
                    "realtimeSupported": True,
                    "syncing": bool(_sync_state["syncing"]),
                    "lastError": _sync_state["lastError"],
                    "fetchedAt": _sync_state["lastFetchedAt"]
                    or (cached.get("fetchedAt") if cached else None),
                    "dataDate": cached.get("dataDate") if cached else None,
                },
            )
            return

        if path == "/api/realtime":
            query = parse_qs(urlparse(self.path).query)
            force_rt = query.get("force", ["0"])[0].lower() in ("1", "true", "yes")
            try:
                payload = get_realtime_quotes(force=force_rt)
                self._send_json(HTTPStatus.OK, payload)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/latest":
            data = load_latest_market_data()
            if data is None:
                try:
                    data = sync_and_embed(reuse_recent_sec=8.0)
                except Exception as exc:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                    return
            self._send_json(HTTPStatus.OK, data)
            return

        if path == "/api/sync":
            self._handle_sync_request()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "未知的端點"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/sync":
            self._handle_sync_request()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "未知的端點"})


def _find_available_port(start_port: int, attempts: int = 10) -> int:
    for p in range(start_port, start_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start_port


def main() -> int:
    parser = argparse.ArgumentParser(description="台股籌碼風險判讀儀表板本機伺服器")
    parser.add_argument("--sync-only", action="store_true", help="僅執行一次官方市場數據同步並更新 HTML")
    parser.add_argument("--force-tdcc", action="store_true", help="強制重新下載集保中心 TDCC 週檔")
    parser.add_argument("--no-startup-sync", action="store_true", help="啟動伺服器時不執行背景自動同步")
    parser.add_argument("--port", type=int, default=8765, help="伺服器監聽埠號（預設 8765）")
    parser.add_argument("--no-browser", action="store_true", help="啟動時不自動開啟瀏覽器")
    args = parser.parse_args()

    if args.sync_only:
        print("[Sync] 正在從台灣證交所、期交所與市場 K 線 API 同步最新數據...", flush=True)
        data = sync_and_embed(force_tdcc=args.force_tdcc)
        print(
            f"[Sync] 同步完成！基準日：{data.get('dateLabel')} | 加權指數：{data.get('metrics', {}).get('twiiClose')}",
            flush=True,
        )
        return 0

    port = _find_available_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"[Server] 儀表板已啟動：{url}", flush=True)
    print(
        "[Server] 已先行載入既有快取，並於背景同步最新官方盤後數據；保持此視窗開啟即可一鍵同步，按 Ctrl+C 結束。",
        flush=True,
    )

    if not args.no_startup_sync:
        _sync_state["syncing"] = True

        def _bg_startup_sync() -> None:
            print("[Startup] 正在背景抓取最新台股官方盤後數據...", flush=True)
            try:
                data = sync_and_embed(force_tdcc=args.force_tdcc)
                print(
                    f"[Startup] 背景同步完成：{data.get('dateLabel')}（{data.get('fetchedAt')}）",
                    flush=True,
                )
            except Exception as exc:
                print(f"[Startup] 背景預抓失敗（將改用既有快取）：{exc}", flush=True)
            finally:
                with _sync_lock:
                    _sync_state["syncing"] = False

        threading.Thread(target=_bg_startup_sync, daemon=True).start()

    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] 收到停止訊號，正在關閉伺服器...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
