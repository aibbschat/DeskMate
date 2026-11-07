"""Bot 群「撤群」的语义契约与宿主弹窗红线。

事故背景（2026-11-07）：桌面 GUI 里点「撤群」毫无反应 —— 没有提示、没有请求、
群也没消失。页面用的是原生 JS 确认框；Tauri/wry 的 WKWebView 设了自定义
UIDelegate（``setUIDelegate``），但该类只实现了文件上传面板、媒体权限与新窗口，
没有 ``runJavaScriptConfirmPanel...``。Apple 文档对未实现的确认面板写明
「No action is taken」/视为取消，于是 ``confirm()`` 静默返回 false，
``if (!confirm(...)) return`` 直接退出。后端 DELETE 实测 200，链路完全正常。

教训不是「补一个 toast」，而是：**内嵌页不能依赖宿主的 JS 弹窗能力**，
且这条交互此前没有任何 DOM 层验证（只有后端 disband_room 的用例）。
"""
from __future__ import annotations

import re
import tempfile

import pytest
from fastapi.testclient import TestClient

from deskmate.bots.web import PAGE, create_app

# 只匹配「调用」，不匹配注释里的名词；页面里连注释都不该出现带括号的写法。
NATIVE_DIALOG = re.compile(r"(?<![\w.])(?:window\s*\.\s*)?(?:confirm|alert|prompt)\s*\(")


def _page(prefix: str = "") -> str:
    with tempfile.TemporaryDirectory() as d:
        return TestClient(create_app(base_dir=d, base_prefix=prefix)).get("/").text


@pytest.mark.parametrize("prefix", ["", "/bots"])
def test_page_never_calls_native_js_dialogs(prefix: str) -> None:
    """两种形态的页面都必须自带确认层，绝不回到宿主 JS 弹窗。"""
    html = _page(prefix)
    assert not NATIVE_DIALOG.findall(html), (
        f"prefix={prefix!r} 页面出现了原生弹窗调用：{NATIVE_DIALOG.findall(html)}"
    )
    assert 'id="disband-dlg"' in html
    assert "askDisband" in html


@pytest.mark.parametrize("prefix", ["", "/bots"])
def test_disband_dialog_spells_out_the_semantics(prefix: str) -> None:
    """确认层必须把用户问的「撤群之后会怎样」说清楚，不能只问一句「确定吗」。"""
    html = _page(prefix)
    for phrase in ("从左侧", "消失", "不能再发言", "留档", "看不回来", "不再复用", "重建"):
        assert phrase in html, f"prefix={prefix!r} 确认层缺少语义说明：{phrase}"


def test_disband_lifecycle_matches_documented_semantics() -> None:
    """把「撤群 = 打墓碑」逐条钉死：列表消失 / 不再能发言 / 历史留档 / id 不复用。"""
    with tempfile.TemporaryDirectory() as d:
        c = TestClient(create_app(base_dir=d))
        bots = c.get("/api/bots").json()
        rid = c.post(
            "/api/rooms",
            json={"topic": "撤群语义验收", "bot_ids": [bots[0]["id"], bots[1]["id"]]},
        ).json()["room_id"]

        assert c.post(f"/api/rooms/{rid}/say", json={"text": "在群里说一句"}).status_code == 200
        history = c.get(f"/api/rooms/{rid}/messages").json()
        assert history, "发言应当落进 ChatStore"

        gone = c.delete(f"/api/rooms/{rid}")
        assert gone.status_code == 200
        assert gone.json()["idempotent"] is False

        # ① 群从「我的群」列表消失
        assert [r["room_id"] for r in c.get("/api/rooms").json()] == []
        # ② 不能再发言、不能读回消息、不能续轮
        assert c.post(f"/api/rooms/{rid}/say", json={"text": "还能说吗"}).status_code == 400
        assert c.get(f"/api/rooms/{rid}/messages").status_code == 404
        assert c.post(
            f"/api/rooms/{rid}/continue", json={"round_index": 1}
        ).status_code == 404
        # ③ 消息只是墓碑化后的留档：bots.db 打标记，chat.db 的行没被物理删除
        from deskmate.bots.rooms import RoomStore

        store = RoomStore(d)
        assert store.get_room(rid) is None
        assert store.get_room(rid, include_disbanded=True) is not None
        assert store.chat_store.messages(rid), "撤群不得物理删除聊天记录"
        # ④ 幂等 + id 永不复用；同话题重建是新群，历史不继承
        assert c.delete(f"/api/rooms/{rid}").json()["idempotent"] is True
        new_id = c.post(
            "/api/rooms",
            json={"topic": "撤群语义验收", "bot_ids": [bots[0]["id"], bots[1]["id"]]},
        ).json()["room_id"]
        assert new_id != rid
        assert c.get(f"/api/rooms/{new_id}/messages").json() == []
