"""The protocol layer: event types, channels, JSON codec round-trips."""
import pytest

from lunamoth.protocol import (
    MUSE, SAY, Notice, TextDelta, ThinkDelta, ToolEnd, ToolStart,
    from_dict, from_json, to_dict, to_json,
)


def test_codec_round_trips_every_event():
    samples = [
        TextDelta("你好", channel=MUSE),
        TextDelta("hi"),  # default channel = say
        ThinkDelta("hmm\n"),
        ToolStart("terminal", preview='{"command": "ls"}', index=2),
        ToolEnd("terminal", ok=False, duration=1.25, summary="⚙ terminal ✗ boom", index=2),
        Notice("retry", "⚠ retry 1/5"),
    ]
    for ev in samples:
        assert from_json(to_json(ev)) == ev


def test_wire_format_is_stable_json():
    d = to_dict(TextDelta("hi"))
    assert d == {"type": "text", "text": "hi", "channel": SAY, "superchat": False}
    assert to_dict(Notice("retry"))["type"] == "notice"


def test_decoder_ignores_unknown_fields_rejects_unknown_types():
    ev = from_dict({"type": "text", "text": "x", "channel": "say", "added_in_v9": True})
    assert ev == TextDelta("x")
    with pytest.raises(KeyError):
        from_dict({"type": "hologram", "text": "x"})


def test_events_are_frozen():
    with pytest.raises(Exception):
        TextDelta("a").text = "b"  # type: ignore[misc]
