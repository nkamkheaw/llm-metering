"""Static checks on the inline page scripts.

These exist because a UI change once shipped with a half-applied patch: the
code that USED a variable landed, the code that DECLARED it did not, and the
Python suite passed because it never touches the frontend. The page rendered
and silently did nothing.
"""

import pathlib
import re

import pytest

UI = pathlib.Path(__file__).resolve().parent.parent / "llm_metering" / "ui"
PAGES = ["index.html", "overview.html"]


def script_of(name: str) -> str:
    html = (UI / name).read_text()
    return html[html.index("<script>") + len("<script>"): html.rindex("</script>")]


def declared(src: str) -> set[str]:
    """Collect identifiers declared in the script.

    Handles comma-separated declarator lists (`let a = 1, b = 2;`), which is
    where a naive regex goes wrong -- and going wrong here means the guard
    reports phantom failures and gets ignored, which is worse than no guard.
    """
    names: set[str] = set()
    for m in re.finditer(r"\b(?:const|let|var)\s", src):
        i = m.end()
        depth = 0
        chunk_start = i
        while i < len(src):
            ch = src[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and ch in ",;\n":
                chunk = src[chunk_start:i]
                ident = re.match(r"\s*([A-Za-z_$][\w$]*)", chunk)
                if ident:
                    names.add(ident.group(1))
                if ch == ";" or ch == "\n" and "=" not in src[i:i + 2]:
                    if ch == ";":
                        break
                chunk_start = i + 1
            i += 1
        else:
            chunk = src[chunk_start:i]
            ident = re.match(r"\s*([A-Za-z_$][\w$]*)", chunk)
            if ident:
                names.add(ident.group(1))
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)", src):
        names.add(m.group(1))
    return names


@pytest.mark.parametrize("page", PAGES)
def test_module_globals_are_declared(page):
    """Every app-level identifier the script uses must also be declared in it.

    This is the exact failure that shipped: `seen` and `runKey` were referenced
    by updateStatus() while their declarations had been dropped by a patch whose
    anchor no longer matched.
    """
    src = script_of(page)
    names = declared(src)
    # Identifiers this app defines for itself. Browser and standard-library
    # globals are deliberately not listed -- those are not what breaks.
    app_globals = {
        "index.html": ["META", "sel", "LAST", "COST", "seen", "simKey", "selectedKeys", "uncachedCount",
                       "timer", "inflight", "estimate", "schedule", "run", "render",
                       "boot", "chips", "cur", "onScenario", "applyState", "copyLink",
                       "readURL", "writeURL", "isProduction", "updateViewBar",
                       "updateStatus", "showProgress", "DEBOUNCE_MS", "tradeoff", "headroom", "latency",
                       "PAL", "G", "el", "f", "matches"],
        "overview.html": ["PALETTE", "G", "pct", "render", "timeline"],
    }[page]
    missing = [n for n in app_globals if n not in names and re.search(rf"\b{re.escape(n)}\b", src)]
    assert not missing, f"{page}: used but never declared: {missing}"


@pytest.mark.parametrize("page", PAGES)
def test_braces_and_parens_balance(page):
    """Cheap syntax smoke test in the absence of a JS engine."""
    src = script_of(page)
    stripped = re.sub(r"`(?:\\.|[^`\\])*`", "``", src)          # template literals
    stripped = re.sub(r"'(?:\\.|[^'\\])*'", "''", stripped)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    for open_c, close_c in [("{", "}"), ("(", ")"), ("[", "]")]:
        assert stripped.count(open_c) == stripped.count(close_c), \
            f"{page}: unbalanced {open_c}{close_c}"


def test_explorer_reads_cost_factor_from_server():
    """The auto-run budget must come from the host's measured speed, not a
    hardcoded local timing -- the instance is ~11x slower than the dev laptop."""
    src = script_of("index.html")
    assert "META.cost_factor" in src, "must seed the factor at boot"
    assert "msg.cost_factor" in src, "must refresh the factor from streamed updates"
    assert "* COST" in src, "estimate() must scale by the server-measured factor"


def test_explorer_renders_incrementally():
    """Rows must be painted as each simulation streams in.

    Computing the whole batch before showing anything is what made a 4-run
    comparison look like a frozen page for 20 seconds.
    """
    src = script_of("index.html")
    assert "/api/run/stream" in src
    assert "getReader()" in src, "must consume the response as a stream"
    assert src.count("render(LAST") >= 2, "must repaint per simulation, not only at the end"


def test_explorer_shows_what_is_still_running():
    """While streaming, the page must say more is coming and name what.

    Progress only in the sidebar goes unread: the eye is on the table where the
    rows are appearing.
    """
    src = script_of("index.html")
    css = (UI / "index.html").read_text()
    assert "still to come" in src, "must state how many runs are outstanding"
    assert "computing…" in src, "the in-flight run must be labelled"
    assert "class=\"progress\"" in src or "className:'progress'" in src
    assert ".spin{" in css, "needs an activity indicator"
    assert "prefers-reduced-motion" in css and ".spin{animation:none" in css, \
        "a spinner that cannot animate must still read as pending"
    assert "plan.slice(series.length)" in src, \
        "pending rows must be derived from what has not arrived yet"


def test_no_auto_run_cap_remains():
    """Streaming plus abort-on-change removed the reason for a cap.

    Measured: a client disconnect stops the server after the simulation already
    in flight, so a superseded selection wastes one simulation rather than the
    whole batch. Gating behind a button would protect against nothing.
    """
    src = script_of("index.html")
    assert "AUTO_BUDGET" not in src, "the cap should be gone, not just raised"
    assert "Too slow to run on every change" not in src
    assert "setTimeout(run, DEBOUNCE_MS)" in src, "every change should still debounce"
