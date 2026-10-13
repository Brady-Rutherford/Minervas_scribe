"""Rendition bookkeeping: ``strict_res`` and ``asr.source_res`` (synthetic data, no network).

The ASR must run on the same rendition the video scan reads, so the transcript records which
one Forum actually served, and ``strict_res=True`` refuses the silent highres/lowres fallback.
"""

import json

import pytest
from test_pipeline_exports import FakeDiarizingASR, scenario

from hosts import cli
from scribe_core.pipeline import (
    PipelineError,
    TranscribeOptions,
    transcribe_bundle,
    transcribe_class,
)

LOW = "https://dexample.cloudfront.net/u/low.mp4?Expires=4102444800&Signature=LOWSIG"
HIGH = "https://dexample.cloudfront.net/u/high.mp4?Expires=4102444800&Signature=HIGHSIG"


class FakeClient:
    """Stands in for ForumClient: only what transcribe_class needs."""

    def __init__(self, urls):
        self.urls = urls
        self.url_calls = 0

    def me(self):
        return {"id": 2, "role": "student"}

    def recording_urls(self, class_id):
        self.url_calls += 1
        return dict(self.urls)


def run_class(tmp_path, urls, **opts):
    bundle, words = scenario()
    asr = FakeDiarizingASR(words)
    client = FakeClient(urls)
    options = TranscribeOptions(formats=("json",), debug_html=False, **opts)
    result = transcribe_class(900001, client, out_dir=tmp_path, options=options, bundle=bundle,
                              provider=asr)
    return result, asr, client


def test_option_defaults():
    o = TranscribeOptions()
    assert (o.res, o.strict_res, o.save_asr) == ("lowres", False, True)


def test_source_res_recorded_in_transcript_and_saved_asr(tmp_path):
    result, asr, client = run_class(tmp_path, {"lowres": LOW, "highres": HIGH}, strict_res=True)
    assert asr.calls[0]["video_url"] == LOW and client.url_calls >= 1
    t = result["transcripts"]["names"]
    assert t["asr"]["source_res"] == "lowres"
    saved = json.loads(next(tmp_path.glob("*_asr.json")).read_text())
    assert saved["source_res"] == "lowres"


def test_highres_requested(tmp_path):
    result, asr, _ = run_class(tmp_path, {"lowres": LOW, "highres": HIGH}, res="highres")
    assert asr.calls[0]["video_url"] == HIGH
    assert result["transcripts"]["names"]["asr"]["source_res"] == "highres"


def test_fallback_without_strict_res_is_recorded(tmp_path):
    result, asr, _ = run_class(tmp_path, {"highres": HIGH})
    assert asr.calls[0]["video_url"] == HIGH
    assert result["transcripts"]["names"]["asr"]["source_res"] == "highres"


def test_strict_res_refuses_the_fallback(tmp_path):
    with pytest.raises(PipelineError, match="strict_res"):
        run_class(tmp_path, {"highres": HIGH}, strict_res=True)


def test_no_url_at_all_still_raises(tmp_path):
    with pytest.raises(PipelineError, match="no recording URL"):
        run_class(tmp_path, {})


def test_transcribe_bundle_takes_an_explicit_source_res(tmp_path):
    bundle, words = scenario()
    result = transcribe_bundle(bundle, out_dir=tmp_path, video_url=LOW,
                               provider=FakeDiarizingASR(words), source_res="lowres",
                               options=TranscribeOptions(formats=("json",), debug_html=False))
    assert result["transcripts"]["names"]["asr"]["source_res"] == "lowres"


def test_unknown_source_res_is_none(tmp_path):
    bundle, words = scenario()
    result = transcribe_bundle(bundle, out_dir=tmp_path, video_url=LOW,
                               provider=FakeDiarizingASR(words),
                               options=TranscribeOptions(formats=("json",), debug_html=False))
    assert result["transcripts"]["names"]["asr"]["source_res"] is None


def test_saved_asr_replay_keeps_its_source_res(tmp_path):
    path = tmp_path / "x_asr.json"
    path.write_text(json.dumps({"provider": "deepgram", "model": "nova-3", "diarized": True,
                                "duration": 10.0, "source_res": "lowres",
                                "words": [{"start": 0.1, "end": 0.5, "text": "Hi", "conf": 0.9,
                                           "cluster": 0}]}))
    bundle, _ = scenario()
    result = transcribe_bundle(bundle, out_dir=tmp_path / "out", provider=cli.SavedASR(path),
                               options=TranscribeOptions(formats=("json",), debug_html=False))
    assert result["transcripts"]["names"]["asr"]["source_res"] == "lowres"
