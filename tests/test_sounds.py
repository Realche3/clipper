from datetime import datetime

import httpx
import pytest
from typer.testing import CliRunner

from clipper import cli, config, llm
from clipper.sounds import fetch, store
from clipper.sounds.fetch import Extracted, ExtractedSound, SoundsError, html_to_text, refresh
from clipper.sounds.store import Sound, SoundsFile, merge, pick_sound, search_url

NOW = datetime(2026, 11, 19, 9, 0)


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRENDING_SOUNDS_FILE", tmp_path / "pinned.yaml")
    monkeypatch.setattr(config, "TRENDING_SOUNDS_JSON", tmp_path / "ws" / "trending.json")


def es(title, artist="A", platform="tiktok", vibe="hype"):
    return ExtractedSound(title=title, artist=artist, platform=platform, vibe=vibe)


# ---------------------------------------------------------------- html -> text


def test_html_to_text_keeps_list_text_and_drops_scripts():
    page = """<html><head><style>.x{}</style><script>var songs = ["fake"];</script></head>
    <body><nav>Home</nav><h2>Trending this week</h2><ol><li>Vogue &amp; More - Madonna</li>
    <li>Thriller - Michael Jackson</li></ol><!-- hidden --></body></html>"""
    text = html_to_text(page)
    assert "Vogue & More - Madonna" in text and "Thriller - Michael Jackson" in text
    assert "fake" not in text and "hidden" not in text and ".x{}" not in text
    assert "Vogue & More - Madonna\n" in text  # list items stay on their own lines


# ---------------------------------------------------------------- store


def test_sound_label_key_and_old_yaml_name():
    s = Sound.model_validate({"name": "Vogue", "vibe": "hype"})
    assert s.title == "Vogue" and s.label == "Vogue"
    assert Sound(title="Vogue", artist="Madonna").label == "Vogue — Madonna"
    assert Sound(title="Don’t Stop!", artist="MJ").key == Sound(title="dont stop", artist="mj").key


def test_merge_dedupes_and_earlier_lists_win():
    pinned = [Sound(title="Vogue", artist="Madonna", source="pinned")]
    fetched = [
        Sound(title="vogue", artist="MADONNA", source="x"),
        Sound(title="Thriller", artist="MJ"),
        Sound(title="  "),
    ]
    out = merge(pinned, fetched)
    assert [s.title for s in out] == ["Vogue", "Thriller"] and out[0].source == "pinned"


def test_pick_sound_rotates_within_vibe_and_falls_back():
    sounds = [Sound(title="A", vibe="hype"), Sound(title="B", vibe="hype"), Sound(title="C")]
    assert [pick_sound(sounds[:2], "hype", i).title for i in range(3)] == ["A", "B", "A"]
    assert pick_sound([Sound(title="A", vibe="hype")], "chill").title == "A"
    assert pick_sound([], "hype") is None


def test_load_pinned_handles_missing_and_broken(tmp_path):
    assert store.load_pinned(tmp_path / "nope.yaml") == []
    bad = tmp_path / "bad.yaml"
    bad.write_text("sounds: [::", encoding="utf-8")
    assert store.load_pinned(bad) == []
    ok = tmp_path / "ok.yaml"
    ok.write_text("sounds:\n  - {title: Vogue, artist: Madonna, vibe: hype}\n", encoding="utf-8")
    [s] = store.load_pinned(ok)
    assert s.source == "pinned" and s.artist == "Madonna"


def test_all_sounds_pinned_first_then_fetched():
    config.TRENDING_SOUNDS_FILE.write_text("sounds:\n  - {title: Pinned}\n", encoding="utf-8")
    store.save_fetched(SoundsFile(fetched_at=NOW, sounds=[Sound(title="Fetched")]))
    assert [s.title for s in store.all_sounds()] == ["Pinned", "Fetched"]


def test_search_url():
    assert search_url("Vogue — Madonna") == "https://www.tiktok.com/search?q=Vogue+Madonna"


# ---------------------------------------------------------------- refresh


@pytest.fixture
def fake_claude(monkeypatch):
    answers = {
        "u1": Extracted(sounds=[es("Vogue", "Madonna"), es("Thriller", "MJ", "instagram")]),
        "u2": Extracted(
            sounds=[es("vogue", "madonna", "both"), es("Fun", "Pharrell", vibe="funny")]
        ),
    }

    def fake(system, prompt, schema, **kw):
        url = prompt.split("\n")[0].removeprefix("Page URL: ")
        if url == "bad-llm":
            raise llm.LLMError("refused")
        return answers[url]

    monkeypatch.setattr(llm, "ask_json", fake)


def fake_fetch(url):
    if url == "bad-http":
        raise httpx.ConnectError("offline")
    return f"page text for {url}"


def test_refresh_merges_sources_and_saves(fake_claude):
    data = refresh(["u1", "bad-http", "u2", "bad-llm"], fetch=fake_fetch, now=NOW)
    assert [s.label for s in data.sounds] == ["Vogue — Madonna", "Thriller — MJ", "Fun — Pharrell"]
    assert data.sounds[0].source == "u1"
    assert [(r.url, r.count, bool(r.error)) for r in data.sources] == [
        ("u1", 2, False),
        ("bad-http", 0, True),
        ("u2", 2, False),
        ("bad-llm", 0, True),
    ]
    saved = store.load_fetched()
    assert saved.fetched_at == NOW and len(saved.sounds) == 3


def test_refresh_with_nothing_keeps_previous_list(fake_claude):
    store.save_fetched(SoundsFile(fetched_at=NOW, sounds=[Sound(title="Old")]))
    with pytest.raises(SoundsError, match="keeping the previous 1"):
        refresh(["bad-http", "bad-llm"], fetch=fake_fetch)
    assert [s.title for s in store.load_fetched().sounds] == ["Old"]


def test_extract_drops_blank_titles(monkeypatch):
    monkeypatch.setattr(
        llm, "ask_json", lambda *a, **k: Extracted(sounds=[es(" "), es(" Ok ", artist=" ")])
    )
    [s] = fetch.extract("u", "text")
    assert s.title == "Ok" and s.artist is None and s.source == "u"


# ---------------------------------------------------------------- CLI


def test_cli_sounds_refresh_and_list(fake_claude, monkeypatch):
    monkeypatch.setattr(config, "TRENDING_SOURCES", ["u1"])
    monkeypatch.setattr(fetch, "fetch_text", fake_fetch)
    runner = CliRunner()
    out = runner.invoke(cli.app, ["sounds", "refresh"])
    assert out.exit_code == 0, out.output
    assert "2 trending sounds saved" in out.output
    out = runner.invoke(cli.app, ["sounds", "list"])
    assert "1. Vogue — Madonna  [tiktok, hype]" in out.output


def test_cli_refresh_failure_exits_nonzero(fake_claude, monkeypatch):
    monkeypatch.setattr(config, "TRENDING_SOURCES", ["bad-llm"])
    out = CliRunner().invoke(cli.app, ["sounds", "refresh"])
    assert out.exit_code == 1


def test_accents_dedupe_and_generic_titles_dropped(monkeypatch):
    assert (
        Sound(title="Ramalama", artist="Róisín Murphy").key
        == Sound(title="ramalama", artist="Roisin Murphy").key
    )
    monkeypatch.setattr(
        llm,
        "ask_json",
        lambda *a, **k: Extracted(
            sounds=[es("Original audio", artist=None), es("Original sound", "hector mort")]
        ),
    )
    [s] = fetch.extract("u", "text")
    assert s.label == "Original sound — hector mort"


def test_extract_uses_the_sounds_model(monkeypatch):
    seen = {}

    def fake(system, prompt, schema, **kw):
        seen.update(kw)
        return Extracted(sounds=[])

    monkeypatch.setattr(config, "CLAUDE_MODEL_SOUNDS", "claude-haiku-5-5")
    monkeypatch.setattr(llm, "ask_json", fake)
    fetch.extract("u", "text")
    assert seen == {"model": "claude-haiku-5-5", "task": "sounds", "effort": "low"}
