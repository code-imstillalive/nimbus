"""Household, 18 Oct 2026 (phone): "When scrolling down user cannot stop and
see data cos page instantly scrolls back up."

The dispatch card rebuilt its whole shadow DOM every 3 s, including the
forecast table's 640px scroll box, so the element under a finger was replaced
mid-swipe. After the first build the card now patches the new markup onto the
existing nodes (`nimbusPatchChildren`). Checked in Chrome against a fake hass
updating every 300 ms: before, `.ftable-wrap` and the mode chips were new
nodes after each rebuild; after, the same nodes, the chart still redrawn and
one click handled per click.

Same day, on an iPhone after v0.94.465: "still resetting. Less dramatic but
definitely does in a very similar way." Two remaining causes: the scroll
restore still wrote scrollTop onto the kept box every rebuild, which stops
iOS's momentum scroll; and matching by position alone meant a section
appearing above the table shifted it onto another node.
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
    # test_only_the_first_build_replaces_the_shadow_dom
    body = _render()
    assert body.count("this.shadowRoot.innerHTML = html") == 1
    assert "nimbusPatchChildren(this.shadowRoot, tpl.content)" in body
    assert body.index("if (!this._patched)") < body.index("nimbusPatchChildren(")

    # test_listeners_are_bound_once_per_node
    body = _render()
    for needle in (
        "if (fresh(btn)) btn.addEventListener('click'",
        "if (fresh(killSwitch)) killSwitch.addEventListener",
        "if (fresh(solveNowBtn)) solveNowBtn.addEventListener",
        "if (!fresh(el)) return;",
    ):
        assert needle in body

    # test_a_kept_scroll_box_is_not_written_to: The restore runs only when the box is a new node.
    body = _render()
    guard = body.index("if (newWrap && newWrap !== prevWrap) {")
    assert guard < body.index("newWrap.scrollTop = prevScrollTop")
    assert guard < body.index("newWrap.scrollLeft = prevScrollLeft")


FAKE_DOM = (
    "class N { constructor(t, name, v){ this.nodeType=t; this.nodeName=name;"
    " this.tagName=name; this.nodeValue=v===undefined?null:v;"
    " this.childNodes=[]; this.attrs=new Map(); }\n"
    "  get attributes(){ return Array.from(this.attrs, ([name,value])=>({name,value})); }\n"
    "  hasAttribute(n){ return this.attrs.has(n); }\n"
    "  getAttribute(n){ return this.attrs.has(n)?this.attrs.get(n):null; }\n"
    "  setAttribute(n,v){ this.attrs.set(n,v); } removeAttribute(n){ this.attrs.delete(n); }\n"
    "  get lastChild(){ return this.childNodes[this.childNodes.length-1]; }\n"
    "  appendChild(c){ this.childNodes.push(c); }\n"
    "  removeChild(c){ this.childNodes.splice(this.childNodes.indexOf(c),1); }\n"
    "  replaceChild(n,o){ this.childNodes[this.childNodes.indexOf(o)]=n; }\n"
    "  insertBefore(n,r){ this.childNodes.splice(this.childNodes.indexOf(r),0,n); }\n"
    "  getRootNode(){ return {}; } }\n"
    "const el=(name,attrs,kids)=>{ const n=new N(1,name);"
    " for(const k in attrs) n.attrs.set(k,attrs[k]); n.childNodes=kids||[]; return n; };\n"
    "const tx=(v)=>new N(3,'#text',v);\n"
)


def _patch_fns() -> str:
    m = re.search(
        r"function nimbusPatchKey\(n\) \{.*?\n\}\n\nfunction nimbusPatchChildren"
        r"\(target, source\) \{.*?\n\}\n",
        _src(),
        re.DOTALL,
    )
    assert m
    return m.group(0)


def _node(script: str) -> str:
    out = subprocess.run(
        ["node", "-e", FAKE_DOM + _patch_fns() + script],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_patch_keeps_matching_nodes_and_replaces_the_rest():
    """Matching nodes keep their identity and get the new text and
    attributes; a changed tag is replaced; surplus children go."""
    out = _node(
        "const wrapOld=el('DIV',{class:'ftable-wrap',old:'1'},[tx('a')]);\n"
        "const target=el('ROOT',{},[wrapOld, el('SPAN',{},[]), el('B',{},[])]);\n"
        "const source=el('ROOT',{},[el('DIV',{class:'ftable-wrap'},[tx('b')]), el('EM',{},[])]);\n"
        "nimbusPatchChildren(target, source);\n"
        "console.log(JSON.stringify({same: target.childNodes[0]===wrapOld,"
        " text: wrapOld.childNodes[0].nodeValue, old: wrapOld.hasAttribute('old'),"
        " second: target.childNodes[1].nodeName, n: target.childNodes.length}));\n"
    )
    assert out == '{"same":true,"text":"b","old":false,"second":"EM","n":2}'


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_section_appearing_above_keeps_the_table_node():
    """A banner that appears before the table, then goes again, never moves
    the table onto another node."""
    out = _node(
        "const wrap=el('DIV',{class:'ftable-wrap'},[tx('rows')]);\n"
        "const target=el('ROOT',{},[el('DIV',{class:'hero'},[]), wrap]);\n"
        "nimbusPatchChildren(target, el('ROOT',{},[el('DIV',{class:'hero'},[]),"
        " el('DIV',{class:'banner'},[tx('stale')]),"
        " el('DIV',{class:'ftable-wrap'},[tx('rows2')])]));\n"
        "const added = target.childNodes[2]===wrap"
        " && target.childNodes[1].getAttribute('class')==='banner';\n"
        "nimbusPatchChildren(target, el('ROOT',{},[el('DIV',{class:'hero'},[]),"
        " el('DIV',{class:'ftable-wrap'},[tx('rows3')])]));\n"
        "console.log(JSON.stringify({added, gone: target.childNodes[1]===wrap,"
        " n: target.childNodes.length, text: wrap.childNodes[0].nodeValue}));\n"
    )
    assert out == '{"added":true,"gone":true,"n":2,"text":"rows3"}'


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_changed_second_class_keeps_the_node():
    """`mode-chip active` -> `mode-chip` is the same chip, patched in place."""
    out = _node(
        "const chip=el('BUTTON',{class:'mode-chip active'},[tx('Auto')]);\n"
        "const target=el('ROOT',{},[chip]);\n"
        "nimbusPatchChildren(target, el('ROOT',{},[el('BUTTON',{class:'mode-chip'},[tx('Auto')])]));\n"
        "console.log(JSON.stringify({same: target.childNodes[0]===chip,"
        " cls: chip.getAttribute('class')}));\n"
    )
    assert out == '{"same":true,"cls":"mode-chip"}'
