from types import SimpleNamespace

import pytest

from scribe_core.asr import (
    PROVIDERS,
    DeepgramASR,
    GroqASR,
    LocalWhisperASR,
    parse_whisper_verbose,
    resolve_provider_name,
    select_provider,
)
from scribe_core.asr_base import ASRError
from scribe_core.fixup import find_shaky_spans, name_fixup
from scribe_core.privacy import ensure_private_dir, load_or_create_salt, pseudonym
from scribe_core.text import join_tokens, transfer_punctuation


def test_registry_and_provider_selection_order():
    assert set(PROVIDERS) == {"deepgram", "groq", "local"}
    assert PROVIDERS["deepgram"].diarizes and not PROVIDERS["groq"].diarizes
    assert resolve_provider_name(None, {"DEEPGRAM_API_KEY": "x", "GROQ_API_KEY": "y"}) == "deepgram"
    assert resolve_provider_name("auto", {"GROQ_API_KEY": "y"}) == "groq"
    assert resolve_provider_name(None, {}) == "local"
    assert resolve_provider_name("groq", {"DEEPGRAM_API_KEY": "x"}) == "groq"  # explicit wins
    with pytest.raises(ASRError):
        resolve_provider_name("whisperx", {})


def test_model_argument_is_respected():
    assert GroqASR("whisper-large-v3").model == "whisper-large-v3"
    assert GroqASR().model == "whisper-large-v3-turbo"
    assert LocalWhisperASR("small").model == "small"
    assert isinstance(select_provider("deepgram", "nova-3"), DeepgramASR)


def test_parse_whisper_verbose_offsets_and_punctuation():
    resp = SimpleNamespace(model_dump=lambda: {
        "language": "en", "duration": 4.0,
        "words": [{"word": "hello", "start": 0.0, "end": 0.4},
                  {"word": "there", "start": 0.5, "end": 0.9},
                  {"word": "um", "start": 1.0, "end": 1.1}],
        "segments": [{"start": 0.0, "end": 1.2, "text": " Hello, there. Um"}],
    })
    words, segs, info = parse_whisper_verbose(resp, offset=100.0)
    assert [w["text"] for w in words] == ["Hello,", "there.", "Um"]
    assert words[0]["start"] == 100.0 and words[2]["filler"] is True
    assert words[0]["cluster"] is None and info["language"] == "en"


def test_transfer_punctuation_leaves_mismatched_segments_alone():
    words = [{"start": 0, "end": 1, "text": "a"}, {"start": 1, "end": 2, "text": "b"}]
    transfer_punctuation(words, [{"start": 0, "end": 2, "text": "A b c."}])
    assert [w["text"] for w in words] == ["a", "b"]
    assert join_tokens(["Hi", ",", "you", "?"]) == "Hi, you?"


def test_groq_needs_audio_file():
    with pytest.raises(ASRError):
        GroqASR(api_key="x").transcribe(video_url="https://x.cloudfront.net/a.mp4")


def test_shaky_spans_need_5s_of_low_confidence():
    words = [{"start": i * 0.5, "end": i * 0.5 + 0.4, "conf": 0.4} for i in range(14)]
    words += [{"start": 8 + i * 0.5, "end": 8.4 + i * 0.5, "conf": 0.95} for i in range(4)]
    words += [{"start": 20, "end": 20.4, "conf": 0.3}]
    spans = find_shaky_spans(words)
    assert len(spans) == 1
    assert spans[0]["start"] == 0 and spans[0]["n_words"] == 14


def test_name_fixup_is_constrained():
    terms = ["Zoltan Example", "Ingrid Bergstrom", "Synthetic Studies"]
    words = [
        {"start": 0, "end": 1, "text": "Zoltin,", "conf": 0.4},      # fixed (dist 1)
        {"start": 1, "end": 2, "text": "Zoltin", "conf": 0.9},       # confident: kept
        {"start": 2, "end": 3, "text": "about", "conf": 0.2},        # common word: kept
        {"start": 3, "end": 4, "text": "Bergstorm", "conf": 0.5},    # fixed (dist 2)
        {"start": 4, "end": 5, "text": "Banana", "conf": 0.1},       # too far: kept
    ]
    out, changes = name_fixup(words, terms)
    assert [w["text"] for w in out] == ["Zoltan,", "Zoltin", "about", "Bergstrom", "Banana"]
    assert [c["to"] for c in changes] == ["Zoltan,", "Bergstrom"]
    assert all(o["start"] == w["start"] for o, w in zip(out, words, strict=True))


def test_pseudonyms_are_salted_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("SCRIBE_ID_SALT", raising=False)
    assert pseudonym(123, "a") == pseudonym("123", "a") != pseudonym(123, "b")
    salt_file = tmp_path / "d" / ".id_salt"
    s1 = load_or_create_salt(salt_file)
    assert load_or_create_salt(salt_file) == s1 and len(s1) == 32
    monkeypatch.setenv("SCRIBE_ID_SALT", "from-env")
    assert load_or_create_salt(salt_file) == "from-env"
    d = ensure_private_dir(tmp_path / "scribe_data")
    assert (d / ".gitignore").read_text().strip().endswith("*")
