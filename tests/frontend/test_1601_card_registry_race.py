"""nimbus #1601: a bundled card must end up in the registry Home Assistant
reads, whichever side of HA's registry swap it loads on.

HA replaces window.customElements with its scoped custom-element-registry
polyfill shortly after the page starts. Measured on the reference household's
production Forecaster tab (cold load, headless Chromium, 7 Oct 2026): the
Forecaster card registered at 61 ms, before the swap, into the browser's
native registry, which HA no longer reads, and the tab showed "Custom element
doesn't exist: nimbus-forecast-card" until a refresh. With the fix served in
place of production's files, the same cold load rendered both charts with no
error card.

These tests run each real card file under Node against a fake registry that
refuses a reused name or constructor (as the polyfill does) and is swapped
after the file has run.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

FE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
)

CARDS = {
    "nimbus-dispatch-card-v4.js": ["nimbus-dispatch-card-v4"],
    "nimbus-forecast-card.js": ["nimbus-forecast-card"],
    "nimbus-regret-card.js": ["nimbus-regret-card"],
    "nimbus-topology-card.js": ["nimbus-topology-card", "switchboard-topology-card"],
}

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, scenario, namesJson] = process.argv.slice(1);
const names = JSON.parse(namesJson);
class Registry {
  constructor(label) { this.label = label; this.byName = new Map(); this.ctors = new Set(); }
  define(name, cls) {
    if (this.byName.has(name)) throw new Error(`name ${name} already used`);
    if (this.ctors.has(cls)) throw new Error("this constructor has already been used");
    this.byName.set(name, cls); this.ctors.add(cls);
  }
  get(name) { return this.byName.get(name); }
  whenDefined() { return new Promise(() => {}); }
}
const native = new Registry("native"), polyfill = new Registry("polyfill");
const timers = [], listeners = [];
class HTMLElement {}
const ctx = {
  HTMLElement, console, Math, JSON, Date, Number, String, Object, Array, Set, Map, Promise,
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
  document: { addEventListener() {}, readyState: "complete", createElement() { return {}; } },
  addEventListener: (ev, fn) => listeners.push(fn),
};
ctx.window = ctx;
ctx.customElements = scenario === "late" ? polyfill : native;
if (scenario === "older_copy") native.define(names[0], class Older extends HTMLElement {});
vm.createContext(ctx);
let threw = null;
try { vm.runInContext(fs.readFileSync(file, "utf8"), ctx, { filename: file }); }
catch (e) { threw = String(e && e.message || e); }
const pushed = (ctx.customCards || []).map((c) => c.type);
// HA installs its polyfill after the card has run.
ctx.customElements = polyfill;
for (const fn of timers.splice(0)) fn();
for (const fn of listeners.splice(0)) fn();
const out = { threw, pushed, names: {} };
for (const n of names) {
  const p = polyfill.get(n), nat = native.get(n);
  out.names[n] = {
    inPolyfill: !!p,
    polyfillIsUsable: !!p && typeof p === "function" && (p.prototype instanceof HTMLElement),
    inNative: !!nat,
  };
}
process.stdout.write(JSON.stringify(out));
"""


def _run(card: str, scenario: str) -> dict:
    out = subprocess.run(
        ["node", "-e", HARNESS, str(FE / card), scenario, json.dumps(CARDS[card])],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


@needs_node
@pytest.mark.parametrize("card", sorted(CARDS))
def test_registered_before_the_swap_still_reaches_has_registry(card: str) -> None:
    """The reference household's case: the card ran first, HA swapped later."""
    result = _run(card, "early")
    assert result["threw"] is None, result
    for name, where in result["names"].items():
        assert where["inNative"], (card, name)
        assert where["inPolyfill"] and where["polyfillIsUsable"], (card, name, where)


@needs_node
@pytest.mark.parametrize("card", sorted(CARDS))
def test_registered_after_the_swap_is_registered_once(card: str) -> None:
    """HA's registry already in place: one registration, nothing thrown when
    the settle-time checks run."""
    result = _run(card, "late")
    assert result["threw"] is None, result
    for name, where in result["names"].items():
        assert where["inPolyfill"], (card, name)
        assert not where["inNative"], (card, name)


@needs_node
@pytest.mark.parametrize("card", sorted(CARDS))
def test_an_older_copy_registered_first_does_not_break_the_file(card: str) -> None:
    """An old hand-copied /local card already took the name: this file must
    still run to the end (its card-picker entry included) and still reach
    HA's registry."""
    result = _run(card, "older_copy")
    assert result["threw"] is None, result
    assert CARDS[card][0] in result["pushed"]
    for name, where in result["names"].items():
        assert where["inPolyfill"], (card, name)


@pytest.mark.parametrize("card", sorted(CARDS))
def test_every_card_registers_through_the_race_safe_helper(card: str) -> None:
    src = (FE / card).read_text(encoding="utf-8")
    for name in CARDS[card]:
        assert (
            f'nimbusRegisterCard("{name}"' in src
            or f"nimbusRegisterCard('{name}'" in src
        )
    # The only bare define left is the helper's own first attempt.
    assert src.count("customElements.define(") == 1
    assert "registry.define(name, class extends cls {})" in src
