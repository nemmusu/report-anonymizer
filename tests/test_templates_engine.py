"""Tests for the report templates registry and renderer (no pandoc needed).

The pure-Python helpers (`list_templates`, `add_user_template`,
`delete_user_template`, `_wrap_html`, `TemplateContext`) are exercised
without invoking pandoc/wkhtmltopdf, so the suite stays fast and runs in CI.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize import templates as tpl


def test_builtin_templates_are_discovered():
    items = tpl.list_templates()
    ids = {t.id for t in items}
    assert {"pentest_modern", "pentest_minimal", "pentest_classic"}.issubset(ids)
    for t in items:
        assert t.wrapper_path.exists(), f"wrapper missing for {t.id}"
        assert t.style_path.exists(), f"style missing for {t.id}"


def test_get_template_known_and_unknown():
    assert tpl.get_template("pentest_modern") is not None
    assert tpl.get_template("__nope__") is None


def test_template_context_to_dict_is_complete():
    ctx = tpl.TemplateContext(
        title="X", subtitle="Y", engagement="E", author="A", classification="C",
        date="2026-05-07", footer="F",
    )
    d = ctx.to_dict()
    assert set(d.keys()) >= {
        "title",
        "subtitle",
        "engagement",
        "author",
        "date",
        "classification",
        "footer",
    }


def test_wrap_html_replaces_all_placeholders():
    t = tpl.get_template("pentest_modern")
    assert t is not None
    ctx = tpl.TemplateContext(title="The Report", subtitle="abc", author="Alice")
    html = tpl._wrap_html(t, body_html="<p>BODY</p>", ctx=ctx)
    assert "The Report" in html
    assert "Alice" in html
    assert "BODY" in html
    assert "{{ title }}" not in html
    assert "{{ body }}" not in html
    assert "{{ style }}" not in html


def test_user_template_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tpl, "USER_TEMPLATES_DIR", tmp_path / "user_templates", raising=True
    )
    meta = tpl.add_user_template(
        "my_clean",
        name="My Clean",
        description="A clean custom template",
        wrapper_html="<html><body>{{ title }}{{ body }}</body></html>",
        style_css="body{color:red}",
    )
    assert meta.id == "my_clean"
    assert meta.source == "user"
    assert meta.wrapper_path.exists()
    assert meta.style_path.exists()

    found = [t for t in tpl.list_templates() if t.id == "my_clean"]
    assert found and found[0].name == "My Clean"

    ok = tpl.delete_user_template("my_clean")
    assert ok is True
    assert tpl.get_template("my_clean") is None


def test_user_template_overrides_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tpl, "USER_TEMPLATES_DIR", tmp_path / "user_templates", raising=True
    )
    tpl.add_user_template(
        "pentest_minimal",
        name="My Minimal",
        description="user override",
        wrapper_html="<html>{{ body }}</html>",
        style_css="body{}",
    )
    found = tpl.get_template("pentest_minimal")
    assert found is not None
    assert found.source == "user"
    assert found.name == "My Minimal"


def test_user_template_invalid_id_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tpl, "USER_TEMPLATES_DIR", tmp_path / "user_templates", raising=True
    )
    with pytest.raises(ValueError):
        tpl.add_user_template(
            "../../etc/passwd",
            name="bad",
            description="bad",
            wrapper_html="x",
            style_css="x",
        )
