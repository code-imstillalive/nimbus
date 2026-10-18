"""Household, 18 Oct 2026 (phone): "When scrolling down user cannot stop and
see data cos page instantly scrolls back up."

The dispatch card rebuilt its whole shadow DOM every 3 s, including the
forecast table's 640px scroll box, so the element under a finger was replaced
mid-swipe. After the first build the card now patches the new markup onto the
existing nodes (`nimbusPatchChildren`). Checked in Chrome against a fake hass
updating every 300 ms: before, `.ftable-wrap` and the mode chips were new
nodes after each rebuild; after, the same nodes, the chart still redrawn and
one click handled per click.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

CARD = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-dispatch-card-v4.js"
)


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def _render() -> str:
    m = re.search(r"\n  _render\(force\) \{(.*?)\n  \}\n", _src(), re.DOTALL)
    assert m
    return m.group(1)


def test_only_the_first_build_replaces_the_shadow_dom():
    body = _render()
    assert body.count("this.shadowRoot.innerHTML = html") == 1
    assert "nimbusPatchChildren(this.shadowRoot, tpl.content)" in body
    assert body.index("if (!this._patched)") < body.index("nimbusPatchChildren(")


def test_listeners_are_bound_once_per_node():
    body = _render()
    for needle in (
        "if (fresh(btn)) btn.addEventListener('click'",
        "if (fresh(killSwitch)) killSwitch.addEventListener",
        "if (fresh(solveNowBtn)) solveNowBtn.addEventListener",
        "if (!fresh(el)) return;",
    ):
        assert needle in body


def _patch_fn() -> str:
    m = re.search(
        r"function nimbusPatchChildren\(target, source\) \{.*?\n\}\n", _src(), re.DOTALL
    )
    assert m
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_patch_keeps_matching_nodes_and_replaces_the_rest():
    """A minimal DOM: matching nodes keep their identity and get the new text
    and attributes; a changed tag is replaced; surplus children go."""
    script = (
        "class N { constructor(t, name, v){ this.nodeType=t; this.nodeName=name; this.tagName=name;"
        " this.nodeValue=v===undefined?null:v; this.childNodes=[]; this.attrs=new Map(); }\n"
        "  get attributes(){ return Array.from(this.attrs, ([name,value])=>({name,value})); }\n"
        "  hasAttribute(n){ return this.attrs.has(n); } getAttribute(n){ return this.attrs.has(n)?this.attrs.get(n):null; }\n"
        "  setAttribute(n,v){ this.attrs.set(n,v); } removeAttribute(n){ this.attrs.delete(n); }\n"
        "  get lastChild(){ return this.childNodes[this.childNodes.length-1]; }\n"
        "  appendChild(c){ this.childNodes.push(c); } removeChild(c){ this.childNodes.splice(this.childNodes.indexOf(c),1); }\n"
        "  replaceChild(n,o){ this.childNodes[this.childNodes.indexOf(o)]=n; } getRootNode(){ return {}; } }\n"
        "const el=(name,attrs,kids)=>{ const n=new N(1,name); for(const k in attrs) n.attrs.set(k,attrs[k]); n.childNodes=kids||[]; return n; };\n"
        "const tx=(v)=>new N(3,'#text',v);\n"
        + _patch_fn()
        + "const wrapOld=el('DIV',{class:'ftable-wrap',old:'1'},[tx('a')]);\n"
        "const target=el('ROOT',{},[wrapOld, el('SPAN',{},[]), el('B',{},[])]);\n"
        "const source=el('ROOT',{},[el('DIV',{class:'ftable-wrap'},[tx('b')]), el('EM',{},[])]);\n"
        "nimbusPatchChildren(target, source);\n"
        "console.log(JSON.stringify({same: target.childNodes[0]===wrapOld, text: wrapOld.childNodes[0].nodeValue,"
        " old: wrapOld.hasAttribute('old'), second: target.childNodes[1].nodeName, n: target.childNodes.length}));\n"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == (
        '{"same":true,"text":"b","old":false,"second":"EM","n":2}'
    )
