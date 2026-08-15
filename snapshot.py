#!/usr/bin/env python3
"""
One loaded view, written out as a single self-contained HTML file. Stdlib only.

Not a CLI. Imported by serve.py, which decides what goes in the payload; this
module only packages it. Nothing here reads the archive and nothing here touches
the device -- it is string work on files already on disk.

What the packaging has to get right:

  - **No second renderer.** web/app.js and web/charts.js are inlined BYTE FOR BYTE
    and fed a payload instead of fetch(), so the exported file looks like the screen
    because it is the screen. tests/test_web.py asserts the inlined bytes match the
    files, because a forked copy would drift and the drift would only ever be seen
    by whoever was sent the file.
  - **Nothing may point at the server.** The reader has no route to the capture
    host: they have an attachment. So every /style.css, /app.js and /favicon.svg
    reference is substituted out, and the substitution is an EXACT tag match that
    raises when it does not fire -- a new <link> in index.html must break the export
    loudly here, not produce a file that silently renders unstyled somewhere else.
  - **Nothing may close the script element.** A note typed by the user, or a
    register description out of the register map, containing "</script>" would end
    the payload early and leave the rest of it on the page as text. JSON is emitted
    with < > & escaped to \\uXXXX, which is legal JSON, identical after parsing, and
    inert inside HTML.
"""
import base64
import html
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

PAYLOAD_VERSION = 1

# The tags substituted out of web/index.html, matched exactly. See the docstring:
# a miss is an error, not a warning.
TITLE_TAG = "<title>SigenStor telemetry</title>"
ICON_TAG = '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
CSS_TAG = '<link rel="stylesheet" href="/style.css">'
CHARTS_TAG = '<script src="/charts.js"></script>'
APP_TAG = '<script src="/app.js"></script>'

# The global the page looks for. web/app.js reads exactly this name; a test pins
# the two together so renaming one without the other fails in the suite rather
# than in a file already sent to someone.
GLOBAL = "SIGEN_SNAPSHOT"


def _read(name):
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _sub(text, tag, replacement, name):
    if tag not in text:
        raise ValueError(
            f"web/index.html no longer contains {tag!r}, so the snapshot export "
            f"cannot inline {name}. Update snapshot.py's tag constants to match.")
    return text.replace(tag, replacement, 1)


def _inline_js(src):
    """A <script> holding src, safe to sit inside HTML.

    The substitution is the guarantee, not the current contents: neither script
    contains "</script" today, and if one grows a string that does, "<\\/script"
    inside JavaScript means the same thing to the parser.
    """
    return "<script>\n" + src.replace("</script", "<\\/script") + "\n</script>"


def json_literal(obj):
    """`obj` as a JS object literal that cannot end the script element.

    Escaping < > & covers "</script" and "<!--"; U+2028 and U+2029 are legal in
    JSON strings but are line terminators to a JavaScript parser, so they would be
    a syntax error inside a script element.

    Compact separators, unlike the wire format: a window is tens of thousands of
    numbers, and json's default ", " costs about a byte each -- 30 KB of spaces in a
    file whose whole purpose is to be small enough to email.
    """
    text = json.dumps(obj, allow_nan=False, default=str, separators=(",", ":"))
    for raw, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                     ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(raw, esc)
    return text


def window_label(start, end):
    """The window as a phrase, in the CAPTURE HOST's local time.

    Same clock as the archive's filenames, the heartbeats and the page's own axes;
    the reader's zone is not the one the data happened in.
    """
    a, b = time.localtime(start), time.localtime(end)
    left = time.strftime("%Y-%m-%d %H:%M", a)
    right = time.strftime("%H:%M" if a[:3] == b[:3] else "%Y-%m-%d %H:%M", b)
    return f"{left} → {right} {time.strftime('%Z', b)}"


def filename(start, end):
    """A name that says which window it holds, and sorts."""
    stamp = "%s-%s" % (time.strftime("%Y%m%dT%H%M", time.localtime(start)),
                       time.strftime("%Y%m%dT%H%M", time.localtime(end)))
    return f"sigen-snapshot-{stamp}.html"


def _header_comment(payload, label):
    """Why this file exists, for whoever opens it in an editor rather than a browser.

    Generated text only -- never the user's note, which could contain "-->".
    """
    meta = payload["api"]["meta"]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z",
                          time.localtime(payload["exported_at"]))
    return (
        "<!--\n"
        "  A snapshot from the Sigenergy local viewer (serve.py). ONE FILE, and it\n"
        "  is complete: the charts, the page code and the data are all inside it.\n"
        "  It fetches nothing, so it opens with no server, no network and no Python.\n"
        "\n"
        f"  window     {label}\n"
        f"  exported   {stamp}\n"
        f"  archive    plan {meta.get('plan_hash')}, manifest {meta.get('manifest')}\n"
        f"  device     {(meta.get('device') or {}).get('model', 'unknown')}\n"
        "\n"
        "  It is STATIC. Nothing in it can be refreshed, and it holds only the\n"
        "  panels and fields that were on screen when it was written -- the live\n"
        "  viewer has more. Regenerate it there: narrow the window, then\n"
        "  \"Save snapshot\".\n"
        "\n"
        "  Read-only by construction: the viewer decodes bytes already on disk and\n"
        "  never opens a Modbus connection to the inverter.\n"
        "-->")


def render(payload):
    """The whole file, as text. `payload` is what serve.py assembled."""
    win = payload["api"]["window"]
    label = window_label(win["start"], win["end"])
    page = _read("index.html")
    icon = base64.b64encode(_read("favicon.svg").encode()).decode()

    page = _sub(page, TITLE_TAG,
                "<title>SigenStor telemetry — snapshot "
                + html.escape(label) + "</title>", "the title")
    page = _sub(page, ICON_TAG,
                '<link rel="icon" type="image/svg+xml" '
                f'href="data:image/svg+xml;base64,{icon}">', "favicon.svg")
    css = _read("style.css")
    if "</style" in css:
        # There is no escape for this one -- a style element ends at the first
        # "</style", full stop -- so it has to be a refusal rather than a fixup.
        raise ValueError("web/style.css contains '</style', which cannot be inlined")
    page = _sub(page, CSS_TAG, "<style>\n" + css + "\n</style>", "style.css")
    # The payload goes in ahead of both scripts: app.js reads the global as it
    # loads, and charts.js has to be defined before app.js runs boot().
    page = _sub(page, CHARTS_TAG,
                "<script>window." + GLOBAL + " = " + json_literal(payload)
                + ";</script>\n" + _inline_js(_read("charts.js")),
                "charts.js")
    page = _sub(page, APP_TAG, _inline_js(_read("app.js")), "app.js")
    return _header_comment(payload, label) + "\n" + page
