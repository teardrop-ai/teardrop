from __future__ import annotations

from scheduling.templating import render_event_prompt


def test_render_substitutes_scalar_placeholders():
    out = render_event_prompt(
        "Tx {{hash}} moved {{amount}} (urgent={{urgent}})",
        {"hash": "0xabc", "amount": 1500, "urgent": True},
        max_chars=1000,
    )
    assert out == "Tx 0xabc moved 1500 (urgent=true)"


def test_render_missing_and_nonscalar_resolve_empty():
    out = render_event_prompt(
        "a={{missing}} b={{nested}} c={{ok}}",
        {"nested": {"deep": 1}, "ok": "yes"},
        max_chars=1000,
    )
    assert out == "a= b= c=yes"


def test_render_event_json_special_key():
    out = render_event_prompt("payload={{event_json}}", {"a": 1, "b": "x"}, max_chars=1000)
    assert out == 'payload={"a":1,"b":"x"}'


def test_render_is_not_str_format_injectable():
    # A str.format-based renderer would expose attribute access here; the regex
    # renderer must treat these as literal text and substitute nothing.
    template = "safe {0.__class__} {a.__class__} {{a}}"
    out = render_event_prompt(template, {"a": "VALUE"}, max_chars=1000)
    assert out == "safe {0.__class__} {a.__class__} VALUE"


def test_render_caps_total_length():
    out = render_event_prompt("{{big}}", {"big": "x" * 50_000}, max_chars=100)
    assert len(out) == 100


def test_render_non_dict_payload_is_safe():
    out = render_event_prompt("v={{value}} j={{event_json}}", ["not", "a", "dict"], max_chars=1000)
    assert out == 'v= j=["not","a","dict"]'
