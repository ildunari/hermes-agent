from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_standalone_bluebubbles_forces_send_only_despite_ingress_config(monkeypatch):
    """A cron/send_message adapter must neither bind nor register a webhook."""
    from gateway.platforms import bluebubbles
    from tools.send_message_tool import _send_bluebubbles

    events = []
    configs = []

    class FakeAdapter:
        def __init__(self, config):
            self.extra = config.extra
            configs.append(dict(self.extra))

        async def connect(self):
            if self.extra.get("webhook_register"):
                events.extend(["bind", "register"])
            return True

        async def send(self, chat_id, message):
            events.append((chat_id, message))
            return SimpleNamespace(success=True, error=None, message_id="message-1")

        async def disconnect(self):
            events.append("disconnect")

    monkeypatch.setattr(bluebubbles, "BlueBubblesAdapter", FakeAdapter)
    monkeypatch.setattr(bluebubbles, "check_bluebubbles_requirements", lambda: True)
    monkeypatch.setenv("BLUEBUBBLES_WEBHOOK_REGISTER", "true")

    ingress_extra = {
        "server_url": "http://owner.invalid",
        "password": "route-secret",
        "webhook_register": True,
    }
    result = await _send_bluebubbles(ingress_extra, "operator-guid", "alarm")

    assert result == {
        "success": True,
        "platform": "bluebubbles",
        "chat_id": "operator-guid",
        "message_id": "message-1",
    }
    assert "bind" not in events and "register" not in events
    assert events == [("operator-guid", "alarm"), "disconnect"]
    assert configs == [{
        "server_url": "http://owner.invalid",
        "password": "route-secret",
        "webhook_register": False,
    }]
    # The caller's live ingress config is not mutated.
    assert ingress_extra["webhook_register"] is True
