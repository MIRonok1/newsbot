import json

import pytest

from newsbot.config import ConfigError, build_config, load_config
from newsbot.state import SEEN_LIMIT, State
from tests.conftest import ROOT, base_raw


def test_real_config_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    cfg = load_config(ROOT / "config.yaml", env_file=tmp_path / "none.env")
    assert [r["key"] for r in cfg.rubrics] == ["russia", "cfa", "ai", "world"]
    assert len(cfg.feeds) == 14
    cfa = next(f for f in cfg.feeds if f.name == "ЦФА")
    assert cfa.google_news and "hl=ru&gl=RU&ceid=RU:ru" in cfa.url and cfa.window_hours == 48
    assert "when%3A2d" in cfa.url
    tok = next(f for f in cfg.feeds if f.name == "Tokenization")
    assert "ceid=US:en" in tok.url
    assert next(f for f in cfg.feeds if f.name.startswith("Банк России, пресс")).priority == "high"
    assert cfg.data_dir == tmp_path / "d"
    assert {m.key: m.alert_pct for m in cfg.markets}["BRENT"] == 4.0


def test_bad_feed_topic():
    raw = base_raw()
    raw["feeds"].append({"name": "X", "url": "https://x", "topic": "sport"})
    with pytest.raises(ConfigError, match="topic"):
        build_config(raw)


def test_bad_market():
    raw = base_raw()
    raw["markets"].append({"key": "Z", "name": "Z", "source": "yahoo"})
    with pytest.raises(ConfigError, match="symbol"):
        build_config(raw)


def test_state_atomic_and_prune(tmp_path):
    p = tmp_path / "s" / "state.json"
    st = State.load(p)
    st.mark_seen("news", [str(i) for i in range(SEEN_LIMIT + 50)])
    st["tg_offset"] = 7
    st.save()
    assert not p.with_suffix(".tmp").exists()
    data = json.loads(p.read_text())
    assert data["tg_offset"] == 7 and len(data["seen_news"]) == SEEN_LIMIT
    assert State.load(p).seen("news").__contains__(str(SEEN_LIMIT + 49))


def test_state_readonly_does_not_write(tmp_path):
    p = tmp_path / "state.json"
    st = State.load(p, readonly=True)
    st["tg_offset"] = 5
    st.save()
    assert not p.exists()


def test_state_corrupt_recovers(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{broken")
    st = State.load(p)
    assert st["tg_offset"] == 0
