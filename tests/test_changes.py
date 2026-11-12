"""变动复核测试：用脚本化版本序列稳定复现“抓取过程中公告被更正”。

每个用例都让本地场景站点先返回旧版本、再返回新版本，验证采集器：
* 复核发现本项目链接增删 / 关键字段变化 / 复核读取失败 → pending + 无有效期限；
* 变化来源写入 ``changes`` 与 ``sources(role=changed)``；
* 无关项目的目录变化不影响本项目；
* 内容稳定时仍按既有更正顺序合并。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from scripts.change_server import ScenarioSite

ROOT = Path(__file__).resolve().parent.parent / "sample-site" / "changed"
CODE = "GSB-2026-004"


def _collect(list_url: str, code: str = CODE) -> dict:
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/collect",
            json={"list_url": list_url, "project_code": code, "timeout": 5},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run(scenario: str, code: str = CODE) -> dict:
    with ScenarioSite(ROOT, scenario) as site:
        return _collect(site.list_url, code)


def _assert_pending_without_deadline(data: dict, kind: str) -> None:
    assert data["status"] == "pending"
    assert data["can_bid"] is False
    assert data["effective_deadline"] is None, "复核发现变化时绝不能返回旧期限"
    assert any(c["kind"] == kind for c in data["changes"])
    changed_sources = [s for s in data["sources"] if s["role"] == "changed"]
    assert changed_sources, "变化来源必须出现在 sources 中"


def test_stable_content_still_merges_in_correction_order():
    data = _run("stable")

    assert data["status"] == "open"
    assert data["can_bid"] is True
    assert data["effective_deadline"].startswith("2026-11-12T09:30:00")
    assert [n["kind"] for n in data["notices"]] == ["original", "correction"]
    assert data["changes"] == []


def test_list_link_added_between_list_and_detail_forces_pending():
    # 第 1 次列表只有原公告+澄清；复核时列表多出“延期更正”链接
    data = _run("link_added")

    _assert_pending_without_deadline(data, "link_added")
    urls = [s["url"] for s in data["sources"] if s["role"] == "changed"]
    assert any("g004-v2-corr.html" in u for u in urls)
    assert any("复核" in w for w in data["warnings"])


def test_list_link_added_after_not_found_first_pass_forces_pending():
    # 第一次列表完全没有本项目（按旧逻辑会返回 not_found）；复核时刚发布延期更正。
    # 复核只改了列表，不重抓旧详情，因此必须凭链接增删本身判为待核实。
    from scripts.change_server import SCENARIOS

    SCENARIOS["late_correction"] = {
        "list": ["list-v1-unrelated-only.html", "list-v2.html"],
        "details": {},
    }
    try:
        data = _run("late_correction")
    finally:
        SCENARIOS.pop("late_correction")

    _assert_pending_without_deadline(data, "link_added")
    assert any("g004-v2-corr.html" in s["url"] for s in data["sources"] if s["role"] == "changed")


def test_list_link_removed_forces_pending():
    from scripts.change_server import SCENARIOS

    # 首次列表含延期更正链接，复核时该链接从列表消失
    SCENARIOS["link_removed"] = {"list": ["list-v2.html", "list-v1.html"], "details": {}}
    try:
        data = _run("link_removed")
    finally:
        SCENARIOS.pop("link_removed")
    _assert_pending_without_deadline(data, "link_removed")
    assert any("g004-v2-corr.html" in s["url"] for s in data["sources"] if s["role"] == "changed")


def test_detail_deadline_changed_on_recheck_forces_pending():
    data = _run("field_changed")

    _assert_pending_without_deadline(data, "field_changed")
    change = next(c for c in data["changes"] if c["kind"] == "field_changed")
    assert "投标截止时间" in change["message"]
    assert change["url"].endswith("g004-v1-original.html")
    # 即使变化，首次抓到的公告明细仍保留供人工追溯
    assert any(n["kind"] == "original" for n in data["notices"])


def test_cancellation_added_on_recheck_forces_pending():
    data = _run("cancel_added")

    _assert_pending_without_deadline(data, "field_changed")
    change = next(c for c in data["changes"] if c["kind"] == "field_changed")
    assert "取消状态" in change["message"]
    assert data["cancelled"] is True


def test_recheck_read_failure_forces_pending():
    # 详情第 1 次可读、复核时 404：无法确认公告未变，不能被当作稳定内容
    data = _run("recheck_failed")

    _assert_pending_without_deadline(data, "recheck_failed")
    assert any("复核读取详情页失败" in w for w in data["warnings"])


def test_detail_recovered_on_recheck_still_forces_pending():
    # 首次失败的详情页在复核时可读：内容在两次读取之间出现过变化，同样需人工核实
    data = _run("recovered")

    assert data["status"] == "pending"
    assert data["effective_deadline"] is None
    assert any(c["kind"] == "recheck_recovered" for c in data["changes"])


def test_unrelated_project_changes_are_ignored():
    # 列表只调整了 GSB-2026-099 等无关项目；本项目内容稳定
    data = _run("unrelated_changed")

    assert data["status"] == "open"
    assert data["can_bid"] is True
    assert data["effective_deadline"].startswith("2026-11-12T09:30:00")
    assert data["changes"] == []
    assert not any(s["role"] == "changed" for s in data["sources"])


def test_unrelated_project_detail_changes_are_ignored():
    # 列表 v1 → v2 只新增了本项目（GSB-2026-004）的链接；
    # 查询另一项目 GSB-2026-099 时不得把该变化算到它头上
    with ScenarioSite(ROOT, "link_added") as site:
        data = _collect(site.list_url, "GSB-2026-099")

    assert data["status"] == "open"
    assert data["changes"] == []
    assert not any(s["role"] == "changed" for s in data["sources"])
