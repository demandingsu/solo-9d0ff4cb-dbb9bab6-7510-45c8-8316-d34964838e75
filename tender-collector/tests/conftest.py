"""共享测试设施：线程内静态 HTTP 服务器（支持故障注入与内容改写）、
独立临时数据库的 FastAPI TestClient，以及采集辅助函数。
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import shutil
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"

# 加载示例数据生成器（脚本名带连字符，用 importlib）
spec = importlib.util.spec_from_file_location(
    "generate_demo_site", ROOT / "scripts" / "generate_demo_site.py"
)
gen = importlib.util.module_from_spec(spec)
sys.modules["generate_demo_site"] = gen
spec.loader.exec_module(gen)


class FlakyHandler(http.server.SimpleHTTPRequestHandler):
    # 类属性：需要返回 404 的路径集合
    fail_paths: set[str] = set()

    def do_GET(self):  # noqa: N802
        if self.path in self.fail_paths:
            self.send_error(404, "injected failure")
            return
        return super().do_GET()

    def log_message(self, *args):  # 静默
        pass


def _serve(directory: Path, handler_cls=FlakyHandler):
    handler = functools.partial(handler_cls, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}"


@pytest.fixture(scope="session")
def demo_server():
    """托管仓库内置的第一轮示例站（只读）。"""
    FlakyHandler.fail_paths = set()
    httpd, base = _serve(DEMO_DIR)
    yield base, httpd, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def mutable_site(tmp_path):
    """把第一轮示例站复制到临时目录后托管，测试可改写其中的详情页，
    模拟“同一详情复采后字段变化”。"""
    site_dir = tmp_path / "site"
    shutil.copytree(DEMO_DIR, site_dir)
    FlakyHandler.fail_paths = set()
    httpd, base = _serve(site_dir)
    yield base, site_dir, FlakyHandler
    httpd.shutdown()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试使用独立临时数据库
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, main, database


def collect(client, base):
    resp = client.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def project_map(payload):
    return {p["project_name"]: p for p in payload["projects"]}
