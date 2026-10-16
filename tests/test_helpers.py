import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "test")
os.environ.setdefault("LIVEKIT_API_SECRET", "test")
os.environ.setdefault("DIALER_API_KEY", "test")


def test_mio_hindi_number():
    pytest.importorskip("fastapi")
    import mio_server as m

    assert m.hindi_number(7) == "सात"
    assert m.hindi_number(100) == "एक सौ"
    assert m.hindi_number(1000) == "एक हज़ार"
    assert m.hindi_number(123) == "एक सौ तेईस"


def test_mio_prepare_numbers_and_loanwords():
    pytest.importorskip("fastapi")
    import mio_server as m

    out = m.prepare("हमारे पास 20 पीस का MOQ है, WhatsApp पर भेजूँ?")
    assert "बीस" in out and "व्हाट्सऐप" in out and "20" not in out


def test_mio_split_clauses_keeps_short_tail_attached():
    pytest.importorskip("fastapi")
    import mio_server as m

    parts = m.split_clauses("जी, हमारी फैक्ट्री नोएडा में है। क्या आप होलसेल में डील करते हैं?")
    assert len(parts) >= 1 and all(p.strip() for p in parts)


def test_mio_take_tags():
    pytest.importorskip("fastapi")
    import mio_server as m

    text, tag = m.take_tags("Hello there <happy>")
    assert text == "Hello there" and tag == "happy"


def test_dialer_normalize():
    pytest.importorskip("livekit.api")
    pytest.importorskip("fastapi")
    import dialer_service as d

    assert d.normalize("9876543210") == ("09876543210", "+919876543210")
    assert d.normalize("+91 98765 43210") == ("09876543210", "+919876543210")
    assert d.normalize("09876543210") == ("09876543210", "+919876543210")
    assert d.normalize("12345") is None
    assert d.normalize("0123456789") is None


def test_agent_e164_and_backchannel():
    pytest.importorskip("livekit.agents")
    import live_agent as a

    assert a.to_e164("919876543210") == "+919876543210"
    assert a.to_e164("abc") == ""
    assert a.is_backchannel("hmm")
    assert a.is_backchannel("हम्म")
    assert not a.is_backchannel("haan bhej do")
