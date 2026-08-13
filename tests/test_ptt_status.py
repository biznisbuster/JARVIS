"""PTT status mora da izlaže auto_send iz config-a (Faza 6, T3)."""

from jarvis.config import SETTINGS
from jarvis.hotkey import PushToTalk


def test_status_includes_auto_send() -> None:
    ptt = PushToTalk()
    status = ptt.status()
    assert "auto_send" in status
    assert isinstance(status["auto_send"], bool)
    assert status["auto_send"] == SETTINGS.audio.push_to_talk.auto_send
