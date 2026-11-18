"""The 3D viewer served by ARGUS: page injection, and playback of a recording."""
import asyncio
import json

import pytest

from argus import config, viewer


def test_page_points_at_the_given_socket_and_loads_the_embed():
    html = viewer.page("/api/runs/x/replay/ws?speed=2", "ARGUS · replay x", "replay", "x", 12.5, True)
    cfg = json.loads(html.split("window.ARGUS_VIEW = ", 1)[1].split(";</script>", 1)[0])
    assert cfg == {"ws": "/api/runs/x/replay/ws?speed=2", "mode": "replay", "run": "x", "duration_s": 12.5,
                   "record": True}
    assert html.index("/static/viewer_embed.js") < html.index("./boot.js")  # before the viewer boots
    assert "{{ ws_port }}" not in html


class FakeSocket:
    def __init__(self):
        self.sent, self.inbox = [], asyncio.Queue()

    async def send_text(self, text):
        self.sent.append(text)
        if text.startswith('{"event": "scene"'):  # the page draws the deck, then says go
            await self.inbox.put('{"event": "hello"}')
            await self.inbox.put('{"event": "argus_go"}')
        if '"kind": "end"' in text:
            await self.inbox.put(None)

    async def receive_text(self):
        msg = await self.inbox.get()
        if msg is None:
            raise RuntimeError("closed")
        return msg


@pytest.mark.anyio
async def test_replay_sends_the_recording_in_order_with_start_and_end_marks():
    run = "20260101-000000-test"
    folder = config.RUNS / run / "ahc"
    folder.mkdir(parents=True, exist_ok=True)
    messages = [[0.0, '{"event": "scene", "data": {}}'], [0.01, '{"event": "state", "data": {"a": 1}}'],
                [0.5, '{"event": "state", "data": {"a": 2}}'], [0.9, '{"event": "moves", "data": {"moves": []}}']]
    (folder / viewer.RECORDING).write_text(json.dumps({"duration_s": 0.9, "meshes": {}, "messages": messages}))
    assert viewer.recording_info(run) == {"duration_s": 0.9, "messages": 4, "gif": False}

    ws = FakeSocket()
    await asyncio.wait_for(viewer.replay(ws, run, speed=8), 5)
    kinds = [json.loads(m)["event"] + (":" + json.loads(m)["data"]["kind"] if '"argus"' in m else "") for m in ws.sent]
    assert kinds == ["scene", "state", "argus:start", "state", "moves", "argus:end"]
