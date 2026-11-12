"""可复现的“抓取过程中公告发生变化”样例站点（仅标准库，可离线运行）。

背景：采购站可能在采集器“抓列表”与“抓详情”之间发布更正。本服务按请求顺序
先返回旧版本文件、再返回新版本文件，从而稳定复现该时序，无需真实外网：

* ``link_added``       列表第 1 次读取为 v1，第 2 次（复核）为新增了延期更正的 v2；
* ``field_changed``    本项目原公告第 1 次为旧截止时间，第 2 次（复核）截止时间被改；
* ``cancel_added``     本项目更正公告在复核时新增“取消采购”；
* ``recheck_failed``   本项目更正公告第 1 次可读，复核时返回 404；
* ``recovered``        本项目更正公告第 1 次 404，复核时已可读（仍按变化处理）；
* ``unrelated_changed`` 列表只变动无关项目（GSB-2026-099）的链接，本项目不变；
* ``stable``           所有内容稳定，用于验证正常合并顺序不受影响。

用法（本地离线演示）::

    python3 scripts/change_server.py --port 8090 --scenario link_added

然后另开终端调用 API（列表页为 http://localhost:8090/list.html，项目 GSB-2026-004）。
"""

from __future__ import annotations

import argparse
import http.server
import threading
from pathlib import Path
from urllib.parse import urlparse

# 详情页名 -> 依次返回的文件名；``None`` 表示该次请求返回 404
Scenario = dict[str, object]

SCENARIOS: dict[str, Scenario] = {
    "stable": {
        "list": ["list-v1.html"],
        "details": {},
    },
    "link_added": {
        "list": ["list-v1.html", "list-v2.html"],
        "details": {},
    },
    "field_changed": {
        "list": ["list-v1.html"],
        "details": {"g004-v1-original.html": ["g004-v1-original.html", "g004-v1-original-updated.html"]},
    },
    "cancel_added": {
        "list": ["list-v1.html"],
        "details": {"g004-v1-corr.html": ["g004-v1-corr.html", "g004-v1-corr-cancelled.html"]},
    },
    "recheck_failed": {
        "list": ["list-v1.html"],
        "details": {"g004-v1-corr.html": ["g004-v1-corr.html", None]},
    },
    "unrelated_changed": {
        "list": ["list-v1.html", "list-v2-unrelated.html"],
        "details": {},
    },
    "recovered": {
        "list": ["list-v1.html"],
        "details": {"g004-v1-corr.html": [None, "g004-v1-corr.html"]},
    },
}


class _State:
    """记录各路径已服务次数，按场景序列选择下一次返回的版本。"""

    def __init__(self, scenario: Scenario) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._scenario = scenario

    def next_file(self, path: str, default: str | None) -> str | None:
        sequence: list[str | None]
        if path == "/list.html":
            sequence = list(self._scenario["list"])  # type: ignore[arg-type]
        else:
            details = self._scenario["details"]  # type: ignore[assignment]
            sequence = list(details.get(Path(path).name) or ([default] if default else []))
        with self._lock:
            index = self._counts.get(path, 0)
            self._counts[path] = index + 1
        if not sequence:
            return default
        return sequence[min(index, len(sequence) - 1)]

    def relative_path(self, path: str, name: str) -> Path:
        """详情页文件名按请求路径下的子目录解析（如 /detail/x.html）。"""
        return Path(path).parent.relative_to("/") / name


def make_handler(root: Path, state: _State) -> type[http.server.BaseHTTPRequestHandler]:
    class ChangeSiteHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # 静默
            pass

        def _send_file(self, name: str) -> None:
            target = root / name
            if not target.is_file():
                self._not_found()
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self) -> None:
            body = (
                "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>404 - 页面不存在</title></head><body><h1>404 页面不存在或已下线</h1>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            # 健康检查不消耗版本序列，避免探活请求提前“用掉”v1 列表页
            if path == "/healthz":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            name = state.next_file(path, default=Path(path).name)
            if name is None:
                self._not_found()
                return
            # 详情页按请求路径下的子目录解析：/detail/x.html → detail/x.html
            self._send_file(state.relative_path(path, name))

    return ChangeSiteHandler


class ScenarioSite:
    """在后台线程启动一个按场景返回版本序列的静态站点。"""

    def __init__(
        self,
        root: Path,
        scenario: str = "link_added",
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景：{scenario}，可选：{', '.join(SCENARIOS)}")
        self.root = Path(root)
        self.scenario = scenario
        state = _State(SCENARIOS[scenario])
        self._state = state
        self._server = http.server.ThreadingHTTPServer(
            (host, port), make_handler(self.root, state)
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "ScenarioSite":
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    @property
    def list_url(self) -> str:
        return self.base_url + "list.html"

    def reset(self) -> None:
        """把各路径的版本计数清零，便于同一进程内重复演示同一场景。"""
        with self._state._lock:
            self._state._counts.clear()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ScenarioSite":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="按请求顺序复现公告变化的样例站点")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "sample-site" / "changed",
    )
    parser.add_argument("--scenario", default="link_added", choices=sorted(SCENARIOS))
    args = parser.parse_args()

    site = ScenarioSite(args.root, args.scenario, args.host, args.port).start()
    print(f"场景 {args.scenario} 已启动：{site.list_url}")
    print("提示：请先调用 API，再刷新浏览器，否则刷新会提前消耗列表页的 v1 版本。")
    try:
        site._thread.join()
    except KeyboardInterrupt:
        site.stop()


if __name__ == "__main__":
    main()
