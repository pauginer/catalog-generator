#!/usr/bin/env python3
"""
Catalog Generator - build a static, searchable catalog website from a spreadsheet.

Single file, Python 3.8+ standard library only. Works on macOS, Linux and Windows.

    python3 catalog.py init my-catalog     # scaffold a new catalog project
    python3 catalog.py build               # generate the site into _site/
    python3 catalog.py serve               # build, serve on localhost and rebuild on changes

See README.md for the template syntax and configuration reference.
"""

import argparse
import csv
import functools
import html
import http.server
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.0.0"
MARKER_FILE = ".catalog-generator"


class CatalogError(Exception):
    """A user-facing error (bad config, missing file, unreachable sheet...)."""


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print("warning: " + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def col_letter(i):
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def letter_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_text(s):
    """Lowercase and accent-insensitive text used for search."""
    return strip_accents(s or "").lower()


def slugify(s, fallback="item"):
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80].strip("-") or fallback


def esc(s):
    """Escape text for HTML without double-escaping existing entities."""
    s = re.sub(r"&(?!#?[A-Za-z0-9]+;)", "&amp;", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


def attr(s):
    return esc(s).replace('"', "&quot;")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s))


# --------------------------------------------------------------------------
# Markdown (a compact CommonMark/GFM subset: headings, paragraphs, emphasis,
# links, images, lists, task lists, blockquotes, code, tables, raw HTML)
# --------------------------------------------------------------------------

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w+-]*).*$")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
HR_RE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
QUOTE_RE = re.compile(r"^ {0,3}> ?(.*)$")
LIST_RE = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(\s+|$)(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)\s*$")
HTML_BLOCK_RE = re.compile(
    r"^ {0,3}<(?:!--|/?(?:address|article|aside|blockquote|details|dialog|div|dl|dt|dd|fieldset|"
    r"figcaption|figure|footer|form|h[1-6]|header|hr|iframe|li|main|nav|ol|p|pre|section|summary|"
    r"table|tbody|td|tfoot|th|thead|tr|ul|video|audio|picture|img|script|style|center)\b)",
    re.I,
)

IMG_RE = re.compile(r'!\[([^\]]*)\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))*)>?(?:\s+"([^"]*)")?\s*\)')
LINK_RE = re.compile(
    r'\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))*)>?(?:\s+"([^"]*)")?\s*\)'
)
AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^\s<>]+)>")
BARE_URL_RE = re.compile(r'(?<![\w"\'=/\x00])https?://[^\s<>"\x00]+')
INLINE_HTML_RE = re.compile(r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s+[^<>]*?)?/?>", re.S)


def md_inline(text):
    stash = []

    def keep(s):
        stash.append(s)
        return "\x00%d\x00" % (len(stash) - 1)

    def bare_url(m):
        url = m.group(0)
        trail = ""
        while url and (url[-1] in ".,;:!?'\"" or (url[-1] == ")" and url.count(")") > url.count("("))):
            trail = url[-1] + trail
            url = url[:-1]
        return keep('<a href="%s">%s</a>' % (attr(url), esc(url))) + trail

    def link(m):
        label = BARE_URL_RE.sub(lambda u: keep(esc(u.group(0))), m.group(1))
        title = ' title="%s"' % attr(m.group(3)) if m.group(3) else ""
        return keep('<a href="%s"%s>' % (attr(m.group(2)), title)) + label + keep("</a>")

    def image(m):
        title = ' title="%s"' % attr(m.group(3)) if m.group(3) else ""
        return keep('<img src="%s" alt="%s"%s loading="lazy">' % (attr(m.group(2)), attr(m.group(1)), title))

    text = re.sub(r"(`+)(.+?)\1", lambda m: keep("<code>%s</code>" % esc(m.group(2).strip())), text, flags=re.S)
    text = re.sub(r"(?:\\| {2,})\n", lambda m: keep("<br>") + "\n", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~<>$])", lambda m: keep(esc(m.group(1))), text)
    text = IMG_RE.sub(image, text)
    text = LINK_RE.sub(link, text)
    text = AUTOLINK_RE.sub(lambda m: keep('<a href="%s">%s</a>' % (attr(m.group(1)), esc(m.group(1)))), text)
    text = INLINE_HTML_RE.sub(lambda m: keep(m.group(0)), text)
    text = BARE_URL_RE.sub(bare_url, text)
    text = esc(text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text, flags=re.S)
    text = re.sub(r"\*(?=\S)(.+?)(?<=\S)\*", r"<em>\1</em>", text, flags=re.S)
    text = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"<em>\1</em>", text, flags=re.S)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text, flags=re.S)
    for _ in range(5):
        if "\x00" not in text:
            break
        text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def _starts_block(line):
    return bool(
        FENCE_RE.match(line) or HEADING_RE.match(line) or HR_RE.match(line)
        or QUOTE_RE.match(line) or HTML_BLOCK_RE.match(line)
    )


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def md_to_html(text):
    lines = text.replace("\r\n", "\n").replace("\t", "    ").split("\n")
    return _blocks(lines, tight=False)


def _blocks(lines, tight):
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = FENCE_RE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            cls = ' class="language-%s"' % attr(lang) if lang else ""
            out.append("<pre><code%s>%s</code></pre>" % (cls, html.escape("\n".join(body), quote=False)))
            continue

        m = HEADING_RE.match(line)
        if m:
            level, content = len(m.group(1)), m.group(2) or ""
            inner = md_inline(content)
            out.append('<h%d id="%s">%s</h%d>' % (level, slugify(strip_tags(inner), "section"), inner, level))
            i += 1
            continue

        if HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if QUOTE_RE.match(line):
            body = []
            while i < n and lines[i].strip() and QUOTE_RE.match(lines[i]):
                body.append(QUOTE_RE.match(lines[i]).group(1))
                i += 1
            out.append("<blockquote>\n%s\n</blockquote>" % _blocks(body, tight=False))
            continue

        if HTML_BLOCK_RE.match(line):
            body = []
            while i < n and lines[i].strip():
                body.append(lines[i])
                i += 1
            out.append("\n".join(body))
            continue

        if "|" in line and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]) and "-" in lines[i + 1]:
            head = _split_row(line)
            aligns = []
            for c in _split_row(lines[i + 1]):
                left, right = c.startswith(":"), c.endswith(":")
                aligns.append("center" if left and right else "right" if right else "left" if left else "")
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1

            def cell(tag, idx, val):
                a = aligns[idx] if idx < len(aligns) else ""
                style = ' style="text-align:%s"' % a if a else ""
                return "<%s%s>%s</%s>" % (tag, style, md_inline(val), tag)

            thead = "<tr>%s</tr>" % "".join(cell("th", j, c) for j, c in enumerate(head))
            tbody = "".join(
                "<tr>%s</tr>" % "".join(cell("td", j, (r[j] if j < len(r) else "")) for j in range(len(head)))
                for r in rows
            )
            out.append('<div class="table-wrap"><table><thead>%s</thead><tbody>%s</tbody></table></div>' % (thead, tbody))
            continue

        m = LIST_RE.match(line)
        if m and (m.group(4).strip() or m.group(3) == ""):
            html_list, i = _list(lines, i)
            out.append(html_list)
            continue

        # Paragraph (with setext heading support)
        para = [line.strip()]
        i += 1
        setext = None
        while i < n and lines[i].strip():
            if SETEXT_RE.match(lines[i]) and len(para) >= 1:
                setext = 1 if lines[i].strip()[0] == "=" else 2
                i += 1
                break
            if _starts_block(lines[i]) or (LIST_RE.match(lines[i]) and LIST_RE.match(lines[i]).group(4).strip()):
                break
            para.append(lines[i] if lines[i].endswith("  ") else lines[i].strip())
            i += 1
        inner = md_inline("\n".join(para).strip())
        if setext:
            out.append('<h%d id="%s">%s</h%d>' % (setext, slugify(strip_tags(inner), "section"), inner, setext))
        elif tight:
            out.append(inner)
        else:
            out.append("<p>%s</p>" % inner)
    return "\n".join(out)


def _list(lines, i):
    n = len(lines)
    first = LIST_RE.match(lines[i])
    base_indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    items = []
    loose = False
    while i < n:
        m = LIST_RE.match(lines[i])
        if not m or len(m.group(1)) != base_indent or m.group(2)[0].isdigit() != ordered:
            break
        content_indent = base_indent + len(m.group(2)) + max(1, len(m.group(3)))
        body = [m.group(4)]
        i += 1
        saw_blank = False
        while i < n:
            ln = lines[i]
            if not ln.strip():
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and _indent(lines[j]) > base_indent:
                    body.append("")
                    saw_blank = True
                    i += 1
                    continue
                break
            ind = _indent(ln)
            if ind > base_indent:
                body.append(ln[min(ind, content_indent):])
                i += 1
                continue
            if LIST_RE.match(ln) or _starts_block(ln) or saw_blank:
                break
            body.append(ln.strip())  # lazy continuation
            i += 1
        # a blank line followed by another sibling item makes the list loose
        if i < n and not lines[i - 1].strip() and LIST_RE.match(lines[i]):
            loose = True
        if saw_blank and any(b == "" for b in body[:-1]):
            loose = True
        items.append(body)
        while i < n and not lines[i].strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            m2 = LIST_RE.match(lines[j]) if j < n else None
            if m2 and len(m2.group(1)) == base_indent and m2.group(2)[0].isdigit() == ordered:
                loose = True
                i = j
            break
    rendered = []
    for body in items:
        while body and not body[-1].strip():
            body.pop()
        check = ""
        if body:
            t = re.match(r"^\[([ xX])\]\s+(.*)$", body[0])
            if t:
                checked = " checked" if t.group(1) in "xX" else ""
                check = '<input type="checkbox" disabled%s> ' % checked
                body[0] = t.group(2)
        rendered.append('<li%s>%s%s</li>' % (' class="task"' if check else "", check, _blocks(body, tight=not loose)))
    tag = "ol" if ordered else "ul"
    start_attr = ' start="%d"' % start if ordered and start != 1 else ""
    return "<%s%s>\n%s\n</%s>" % (tag, start_attr, "\n".join(rendered), tag), i


# --------------------------------------------------------------------------
# Template engine
#   $A  ${A}  ${Column name}  {{A}}  {{Column name}}  $title  $url ...
#   {{#if A}} ... {{else}} ... {{/if}}      {{#if A == "x"}} ... {{/if}}
#   {{#unless A}} ... {{/unless}}           {{#each Tags}} $this {{/each}}
#   $$ -> a literal dollar sign
# --------------------------------------------------------------------------

BLOCK_TAG_RE = re.compile(r"\{\{\s*(#if|#unless|#each|/if|/unless|/each|else)\b\s*([^}]*?)\s*\}\}")
SUB_RE = re.compile(
    r"\$\$|\$\{([^{}\n]+)\}|\$([A-Z]{1,3}|[a-z][a-z0-9_]*)(?![A-Za-z0-9_])|\{\{\s*([^{}#/\s][^{}\n]*?)\s*\}\}"
)
COND_RE = re.compile(r'^(.+?)\s*(==|!=)\s*"(.*)"$')


class TemplateError(CatalogError):
    pass


class Raw(str):
    """A value that is already HTML and must not be escaped."""


def parse_template(text, name="template"):
    root = []
    stack = [("root", root, None)]
    pos = 0
    for m in BLOCK_TAG_RE.finditer(text):
        start, end = m.start(), m.end()
        # Standalone tags (alone on their line) swallow their own line break.
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end == -1 else line_end
        if not text[line_start:start].strip() and not text[end:line_end].strip():
            start = max(line_start, pos)
            end = min(line_end + 1, len(text))
        cur = stack[-1][1]
        if start > pos:
            cur.append(text[pos:start])
        pos = end
        tag, arg = m.group(1), m.group(2).strip()
        if tag in ("#if", "#unless", "#each"):
            if not arg:
                raise TemplateError("%s: {{%s}} needs a column, e.g. {{%s A}}" % (name, tag, tag))
            node = {"kind": tag[1:], "arg": arg, "body": [], "else": []}
            cur.append(node)
            stack.append((tag[1:], node["body"], node))
        elif tag == "else":
            if len(stack) == 1:
                raise TemplateError("%s: {{else}} outside of an {{#if}} / {{#each}} block" % name)
            kind, _, node = stack[-1]
            stack[-1] = (kind, node["else"], node)
        else:
            kind = tag[1:]
            if stack[-1][0] != kind:
                raise TemplateError("%s: unexpected {{/%s}}" % (name, kind))
            stack.pop()
    if pos < len(text):
        stack[-1][1].append(text[pos:])
    if len(stack) > 1:
        raise TemplateError("%s: {{#%s %s}} is never closed" % (name, stack[-1][0], stack[-1][2]["arg"]))
    return root


class Context:
    def __init__(self, headers, values, specials, separator=",", warned=None):
        self.headers = headers
        self.values = values
        self.specials = specials
        self.separator = separator
        self.warned = warned if warned is not None else set()
        self._by_header = {h.strip().lower(): i for i, h in reversed(list(enumerate(headers))) if h.strip()}

    def child(self, **extra):
        specials = dict(self.specials)
        specials.update(extra)
        return Context(self.headers, self.values, specials, self.separator, self.warned)

    def lookup(self, name):
        """Returns the value (str or Raw) or None when the name is unknown."""
        name = name.strip()
        if name.startswith("$"):
            name = name[1:]
        if name.startswith("{") and name.endswith("}"):
            name = name[1:-1].strip()
        if name in self.specials:
            v = self.specials[name]
            return v if isinstance(v, str) else ("" if v is None else str(v))
        if re.fullmatch(r"[A-Z]{1,3}", name):
            idx = letter_index(name)
            if idx < len(self.headers):
                return self.values[idx] if idx < len(self.values) else ""
        idx = self._by_header.get(name.lower())
        if idx is not None:
            return self.values[idx] if idx < len(self.values) else ""
        return None

    def list(self, name):
        v = self.lookup(name)
        if not v:
            return []
        return [p.strip() for p in v.split(self.separator) if p.strip()] if self.separator else [v]

    def note_unknown(self, ref):
        if ref not in self.warned:
            self.warned.add(ref)
            warn("unknown template variable %s (left as-is)" % ref)


def _condition(arg, ctx):
    m = COND_RE.match(arg)
    if m:
        value = ctx.lookup(m.group(1))
        value = (value or "").strip()
        equal = value.lower() == m.group(3).strip().lower()
        return equal if m.group(2) == "==" else not equal
    value = ctx.lookup(arg)
    if value is None:
        ctx.note_unknown(arg)
    return bool(value and value.strip())


def substitute(text, ctx):
    def rep(m):
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(2) or m.group(3)
        value = ctx.lookup(name)
        if value is None:
            if m.group(2) is None or re.fullmatch(r"[A-Z]{1,3}", name or ""):
                ctx.note_unknown(m.group(0))
            return m.group(0)
        return value if isinstance(value, Raw) else esc(value)

    return SUB_RE.sub(rep, text)


def render_nodes(nodes, ctx):
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(substitute(node, ctx))
        elif node["kind"] in ("if", "unless"):
            ok = _condition(node["arg"], ctx)
            if node["kind"] == "unless":
                ok = not ok
            out.append(render_nodes(node["body"] if ok else node["else"], ctx))
        else:
            values = ctx.list(node["arg"])
            if not values:
                out.append(render_nodes(node["else"], ctx))
            for k, v in enumerate(values):
                sub = ctx.child(this=v, index=str(k + 1), first="1" if k == 0 else "", last="1" if k == len(values) - 1 else "")
                out.append(render_nodes(node["body"], sub))
    return "".join(out)


def render_markdown_template(nodes, ctx):
    return md_to_html(render_nodes(nodes, ctx))


# --------------------------------------------------------------------------
# Data loading: local CSV/TSV or Google Sheets
# --------------------------------------------------------------------------

def google_sheet_csv_url(url, sheet=None):
    """Turn any Google Sheets link into its CSV export URL."""
    if "docs.google.com/spreadsheets" not in url:
        return url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    frag = urllib.parse.parse_qs(parsed.fragment)
    gid = (query.get("gid") or frag.get("gid") or [None])[0]
    m = re.search(r"/spreadsheets/d/e/([\w-]+)", url)
    if m:  # "Publish to the web" link
        csv_url = "https://docs.google.com/spreadsheets/d/e/%s/pub?output=csv" % m.group(1)
        return csv_url + ("&gid=%s" % gid if gid else "")
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if not m:
        return url
    if "output=csv" in url or "format=csv" in url:
        return url
    if sheet:
        return "https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=%s" % (
            m.group(1), urllib.parse.quote(sheet))
    return "https://docs.google.com/spreadsheets/d/%s/export?format=csv%s" % (
        m.group(1), "&gid=%s" % gid if gid else "")


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "catalog-generator/" + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
    except urllib.error.HTTPError as e:
        hint = ""
        if "google.com" in url and e.code in (401, 403, 404):
            hint = ("\n  Make sure the sheet is shared as 'Anyone with the link can view' "
                    "or published via File > Share > Publish to web.")
        raise CatalogError("could not download %s (HTTP %s)%s" % (url, e.code, hint))
    except urllib.error.URLError as e:
        raise CatalogError("could not download %s (%s)" % (url, e.reason))
    text = data.decode("utf-8-sig", errors="replace")
    if "text/html" in ctype or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        raise CatalogError(
            "%s returned a web page instead of CSV data.\n"
            "  If this is a Google Sheet, share it as 'Anyone with the link can view' "
            "or publish it to the web." % url)
    return text


def read_table(source, base_dir, delimiter=None, sheet=None):
    """Return (headers, rows) from a CSV/TSV file path or URL."""
    if re.match(r"^https?://", source):
        url = google_sheet_csv_url(source, sheet)
        log("Downloading %s" % url)
        text = fetch_text(url)
    else:
        path = (base_dir / source).resolve()
        if not path.exists():
            raise CatalogError("data file not found: %s" % path)
        text = path.read_text(encoding="utf-8-sig")
        if delimiter is None and path.suffix.lower() in (".tsv", ".tab"):
            delimiter = "\t"
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise CatalogError("the spreadsheet %s is empty" % source)
    headers = [h.strip() for h in rows[0]]
    width = max(len(r) for r in rows)
    headers += [col_letter(i) for i in range(len(headers), width)]
    headers = [h or col_letter(i) for i, h in enumerate(headers)]
    data = [[c.strip() for c in r] + [""] * (width - len(r)) for r in rows[1:]]
    return headers, data


def resolve_column(ref, headers, what="column"):
    if ref is None or ref == "":
        return None
    ref = str(ref).strip().lstrip("$").strip("{}")
    if re.fullmatch(r"[A-Z]{1,3}", ref) and letter_index(ref) < len(headers):
        return letter_index(ref)
    for i, h in enumerate(headers):
        if h.strip().lower() == ref.lower():
            return i
    raise CatalogError("%s '%s' not found. Available columns: %s" % (
        what, ref, ", ".join("%s (%s)" % (col_letter(i), h) for i, h in enumerate(headers))))


# --------------------------------------------------------------------------
# Site generation
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "title": "My Catalog",
    "description": "",
    "language": "en",
    "source": "data/items.csv",
    "sheet": None,
    "delimiter": None,
    "output": "_site",
    "title_column": "A",
    "id_column": None,
    "list_separator": ",",
    "search_columns": [],
    "filters": [],
    "sort": [],
    "nav": [],
    "footer": "",
    "head": "",
    "templates_dir": "templates",
    "pages_dir": "pages",
    "static_dir": "static",
}


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise CatalogError("config file not found: %s\n  Run 'python3 catalog.py init <folder>' to create a project." % path)
    try:
        user = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise CatalogError("%s: invalid JSON at line %d, column %d: %s" % (path, e.lineno, e.colno, e.msg))
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    return cfg


def load_template(cfg, base, name):
    p = base / cfg["templates_dir"] / name
    if p.exists():
        return p.read_text(encoding="utf-8-sig")
    return DEFAULT_TEMPLATES[name]


def fill_layout(layout, values):
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), layout)


def prepare_output(out, base, force):
    out = out.resolve()
    if out == base or out in base.parents:
        raise CatalogError("the output folder cannot be the project folder or one of its parents: %s" % out)
    if out.exists():
        if any(out.iterdir()) and not (out / MARKER_FILE).exists() and not force:
            raise CatalogError(
                "output folder %s already exists and was not created by catalog-generator.\n"
                "  Use --force to replace its contents." % out)
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / MARKER_FILE).write_text("Generated by catalog-generator. This folder is replaced on every build.\n")
    (out / ".nojekyll").write_text("")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def nav_html(cfg, root):
    links = []
    for entry in cfg.get("nav") or []:
        url = entry.get("url", "")
        if not re.match(r"^([a-z]+:|/|#)", url):
            url = root + url
        links.append('<a href="%s">%s</a>' % (attr(url), esc(entry.get("label", url))))
    return '<nav class="site-nav">%s</nav>' % "".join(links) if links else ""


def number_or_none(v):
    m = re.search(r"-?\d+(?:[.,]\d+)?", v or "")
    return float(m.group(0).replace(",", ".")) if m else None


def natural_key(s):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", normalize_text(s))]


def build(config_path, out_override=None, source_override=None, force=False):
    started = time.time()
    config_path = Path(config_path).resolve()
    base = config_path.parent
    cfg = load_config(config_path)
    source = source_override or os.environ.get("CATALOG_SOURCE") or cfg["source"]
    headers, rows = read_table(source, base, cfg.get("delimiter"), cfg.get("sheet"))
    sep = cfg.get("list_separator") or ""

    title_col = resolve_column(cfg.get("title_column") or "A", headers, "title_column")
    id_col = resolve_column(cfg.get("id_column"), headers, "id_column") if cfg.get("id_column") else None
    search_cols = [resolve_column(c, headers, "search column") for c in cfg.get("search_columns") or []] \
        or list(range(len(headers)))
    filters = []
    for f in cfg.get("filters") or []:
        f = {"column": f} if isinstance(f, str) else dict(f)
        idx = resolve_column(f.get("column"), headers, "filter column")
        filters.append({"idx": idx, "label": f.get("label") or headers[idx],
                        "multiple": bool(f.get("multiple")), "key": slugify(f.get("label") or headers[idx], "f")})
    sorts = []
    for s in cfg.get("sort") or []:
        s = {"column": s} if isinstance(s, str) else dict(s)
        idx = resolve_column(s.get("column"), headers, "sort column")
        sorts.append({"idx": idx, "label": s.get("label") or headers[idx],
                      "numeric": bool(s.get("numeric")), "desc": bool(s.get("descending"))})

    # Items and their URLs
    items, used = [], set()
    for n, values in enumerate(rows):
        raw_id = values[id_col] if id_col is not None else values[title_col]
        slug = slugify(raw_id, "item-%d" % (n + 1))
        candidate, k = slug, 2
        while candidate in used:
            candidate, k = "%s-%d" % (slug, k), k + 1
        used.add(candidate)
        items.append({"n": n, "values": values, "slug": candidate, "title": values[title_col] or "Item %d" % (n + 1)})

    out = Path(out_override) if out_override else base / cfg["output"]
    if not out.is_absolute():
        out = (Path.cwd() / out) if out_override else out
    prepare_output(out, base, force)

    templates = {}
    for name in ("index.md", "card.md", "item.md", "page.md"):
        templates[name] = parse_template(load_template(cfg, base, name), name)
    layout = load_template(cfg, base, "layout.html")
    custom_css_path = base / cfg["templates_dir"] / "style.css"
    css = DEFAULT_CSS + ("\n/* ---- templates/style.css ---- */\n" + custom_css_path.read_text(encoding="utf-8")
                         if custom_css_path.exists() else "")
    write(out / "assets" / "style.css", css)
    write(out / "assets" / "catalog.js", DEFAULT_JS)

    static = base / cfg["static_dir"]
    if static.is_dir():
        shutil.copytree(str(static), str(out), dirs_exist_ok=True)

    warned = set()
    site_title = cfg["title"]
    globals_ = {"site_title": site_title, "count": str(len(items)), "description": cfg.get("description") or ""}

    def page(content, root, page_title, page_class):
        return fill_layout(layout, {
            "content": content,
            "root": root,
            "home": root + "index.html",
            "lang": attr(cfg.get("language") or "en"),
            "page_title": esc(page_title),
            "site_title": esc(site_title),
            "description": attr(cfg.get("description") or ""),
            "nav": nav_html(cfg, root),
            "footer": md_inline(esc(cfg.get("footer") or "")),
            "head": cfg.get("head") or "",
            "page_class": page_class,
        })

    def item_ctx(item, root, url):
        specials = dict(globals_)
        specials.update({
            "title": item["title"], "id": item["slug"], "slug": item["slug"],
            "row": str(item["n"] + 2), "index": str(item["n"] + 1),
            "url": url, "root": root, "home": root + "index.html",
        })
        return Context(headers, item["values"], specials, sep, warned)

    # Detail pages
    for k, item in enumerate(items):
        prev_item = items[k - 1] if k > 0 else None
        next_item = items[k + 1] if k + 1 < len(items) else None
        href = lambda it: urllib.parse.quote(it["slug"]) + ".html" if it else ""
        ctx = item_ctx(item, "../", href(item))
        ctx.specials.update({
            "prev_url": href(prev_item), "prev_title": prev_item["title"] if prev_item else "",
            "next_url": href(next_item), "next_title": next_item["title"] if next_item else "",
            "fields": Raw(fields_html(headers, item["values"])),
            "pager": Raw(pager_html(prev_item, next_item, href)),
        })
        body = render_markdown_template(templates["item.md"], ctx)
        write(out / "items" / (item["slug"] + ".html"),
              page(body, "../", "%s - %s" % (item["title"], site_title), "page-item"))

    # Overview page
    cards = []
    index_data = []
    for item in items:
        ctx = item_ctx(item, "", "items/%s.html" % urllib.parse.quote(item["slug"]))
        cards.append('<article class="card" data-i="%d">\n%s\n</article>' % (
            item["n"], render_markdown_template(templates["card.md"], ctx)))
        v = item["values"]
        index_data.append({
            "s": normalize_text(" ".join(v[i] for i in search_cols)),
            "f": [split_values(v[f["idx"]], sep if f["multiple"] else "") for f in filters],
            "o": [number_or_none(v[s["idx"]]) if s["numeric"] else normalize_text(v[s["idx"]]) for s in sorts],
        })
    widget = catalog_widget(items, filters, sorts, cards, index_data, sep)
    ctx = Context(headers, [], dict(globals_, root="", home="index.html", catalog=Raw("\n<!--catalog-widget-->\n")), sep, warned)
    body = render_markdown_template(templates["index.md"], ctx)
    if "<!--catalog-widget-->" in body:
        body = re.sub(r"(<p>)?\s*<!--catalog-widget-->\s*(</p>)?", lambda m: widget, body, count=1)
    else:
        body += "\n" + widget
    write(out / "index.html", page(body, "", site_title, "page-index"))

    # Extra pages: pages/*.md -> <name>.html
    pages_dir = base / cfg["pages_dir"]
    n_pages = 0
    if pages_dir.is_dir():
        for p in sorted(pages_dir.glob("*.md")):
            ctx = Context(headers, [], dict(globals_, root="", home="index.html"), sep, warned)
            nodes = parse_template(p.read_text(encoding="utf-8-sig"), p.name)
            inner = render_markdown_template(nodes, ctx)
            m = re.search(r"<h1[^>]*>(.*?)</h1>", inner, re.S)
            ptitle = strip_tags(m.group(1)) if m else p.stem.replace("-", " ").title()
            pctx = Context(headers, [], dict(globals_, root="", home="index.html", content=Raw(inner), title=ptitle), sep, warned)
            body = render_markdown_template(templates["page.md"], pctx)
            write(out / (slugify(p.stem, "page") + ".html"), page(body, "", "%s - %s" % (ptitle, site_title), "page-extra"))
            n_pages += 1

    log("Built %d item pages%s into %s (%.2fs)" % (
        len(items), " and %d extra pages" % n_pages if n_pages else "", out, time.time() - started))
    return out


def split_values(value, sep):
    if not value:
        return []
    if not sep:
        return [value]
    return [p.strip() for p in value.split(sep) if p.strip()]


def fields_html(headers, values):
    rows = []
    for h, v in zip(headers, values):
        if v.strip():
            rows.append("<dt>%s</dt><dd>%s</dd>" % (esc(h), md_inline(esc(v)).replace("\n", "<br>")))
    return '<dl class="fields">%s</dl>' % "".join(rows)


def pager_html(prev_item, next_item, href):
    parts = []
    if prev_item:
        parts.append('<a class="prev" href="%s" rel="prev"><span>&larr; Previous</span>%s</a>' % (
            attr(href(prev_item)), esc(prev_item["title"])))
    else:
        parts.append("<span></span>")
    if next_item:
        parts.append('<a class="next" href="%s" rel="next"><span>Next &rarr;</span>%s</a>' % (
            attr(href(next_item)), esc(next_item["title"])))
    return '<nav class="pager">%s</nav>' % "".join(parts)


def catalog_widget(items, filters, sorts, cards, index_data, sep):
    controls = ['<input type="search" class="catalog-search" placeholder="Search %d items..." '
                'aria-label="Search" data-search>' % len(items)]
    for fi, f in enumerate(filters):
        counts = {}
        for d in index_data:
            for v in d["f"][fi]:
                counts[v] = counts.get(v, 0) + 1
        options = "".join('<option value="%s">%s (%d)</option>' % (attr(v), esc(v), counts[v])
                          for v in sorted(counts, key=natural_key))
        controls.append(
            '<label class="catalog-filter"><span>%s</span><select data-filter="%d" data-key="%s">'
            '<option value="">All</option>%s</select></label>' % (esc(f["label"]), fi, attr(f["key"]), options))
    if sorts:
        options = "".join('<option value="%d">%s</option>' % (k, esc(s["label"])) for k, s in enumerate(sorts))
        controls.append('<label class="catalog-filter"><span>Sort</span><select data-sort>'
                        '<option value="">Default</option>%s</select></label>' % options)
    data_json = json.dumps(index_data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    sort_json = json.dumps([{"numeric": s["numeric"], "desc": s["desc"]} for s in sorts])
    return (
        '<section class="catalog" data-catalog data-sorts="%s">\n'
        '<div class="catalog-controls">%s</div>\n'
        '<div class="catalog-status"><span data-count>%d items</span>'
        '<button type="button" class="catalog-reset" data-reset hidden>Clear filters</button></div>\n'
        '<div class="catalog-list" data-list>\n%s\n</div>\n'
        '<p class="catalog-empty" data-empty hidden>No items match your search.</p>\n'
        '<script type="application/json" data-catalog-index>%s</script>\n'
        '</section>' % (attr(sort_json), "".join(controls), len(items), "\n".join(cards), data_json)
    )


# --------------------------------------------------------------------------
# Built-in defaults (used when a project does not provide its own file)
# --------------------------------------------------------------------------

DEFAULT_TEMPLATES = {
    "index.md": """# $site_title

$description

{{catalog}}
""",
    "card.md": """### [$title]($url)

$B
""",
    "item.md": """[&larr; Back to the catalog]($home)

# $title

{{fields}}

{{pager}}
""",
    "page.md": """[&larr; Back to the catalog]($home)

{{content}}
""",
    "layout.html": """<!doctype html>
<html lang="{{lang}}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{page_title}}</title>
<meta name="description" content="{{description}}">
<link rel="stylesheet" href="{{root}}assets/style.css">
{{head}}
</head>
<body class="{{page_class}}">
<header class="site-header">
  <div class="wrap">
    <a class="site-title" href="{{home}}">{{site_title}}</a>
    {{nav}}
  </div>
</header>
<main class="wrap">
{{content}}
</main>
<footer class="site-footer"><div class="wrap">{{footer}}</div></footer>
<script src="{{root}}assets/catalog.js" defer></script>
</body>
</html>
""",
}

DEFAULT_CSS = r""":root {
  --bg: #f7f6f3; --surface: #ffffff; --fg: #1e1f22; --muted: #66676d; --line: #e3e1db;
  --accent: #2f5bd3; --accent-soft: #e9eefb; --tag: #f0efe9; --radius: 12px; --wrap: 1120px;
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 4px 16px rgba(0,0,0,.04);
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #131416; --surface: #1c1d21; --fg: #ebebef; --muted: #a2a3ab; --line: #2d2e34;
          --accent: #8eaaff; --accent-soft: #232a3d; --tag: #26272c; --shadow: none; }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif; }
a { color: var(--accent); text-underline-offset: .15em; }
img { max-width: 100%; height: auto; border-radius: 8px; }
h1, h2, h3, h4 { line-height: 1.25; margin: 1.4em 0 .5em; }
h1 { font-size: clamp(1.8rem, 4vw, 2.5rem); letter-spacing: -.02em; }
code { background: var(--tag); padding: .1em .35em; border-radius: 4px; font-size: .9em; }
pre { background: var(--tag); padding: 1em; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { margin: 1em 0; padding: .2em 1em; border-left: 3px solid var(--line); color: var(--muted); }
hr { border: 0; border-top: 1px solid var(--line); margin: 2em 0; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border-bottom: 1px solid var(--line); padding: .5em .75em; text-align: left; vertical-align: top; }
.wrap { max-width: var(--wrap); margin: 0 auto; padding: 0 16px; }
main.wrap { padding-bottom: 3rem; min-height: 70vh; }

.site-header { background: var(--surface); border-bottom: 1px solid var(--line); }
.site-header .wrap { display: flex; align-items: center; gap: 1.5rem; min-height: 60px; flex-wrap: wrap; }
.site-title { font-weight: 700; color: var(--fg); text-decoration: none; font-size: 1.1rem; }
.site-nav { display: flex; gap: 1rem; margin-left: auto; }
.site-nav a { color: var(--muted); text-decoration: none; }
.site-nav a:hover { color: var(--accent); }
.site-footer { border-top: 1px solid var(--line); color: var(--muted); font-size: .875rem; padding: 1.5rem 0; }
.site-footer:empty, .site-footer .wrap:empty { display: none; }

/* Overview */
.catalog-controls { display: flex; flex-wrap: wrap; gap: .75rem; align-items: end; margin: 1.5rem 0 .75rem; }
.catalog-search { flex: 1 1 260px; font: inherit; padding: .65rem .9rem; border: 1px solid var(--line);
                  border-radius: 10px; background: var(--surface); color: var(--fg); }
.catalog-search:focus, .catalog-filter select:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.catalog-filter { display: flex; flex-direction: column; gap: .2rem; font-size: .8rem; color: var(--muted); }
.catalog-filter select { font: inherit; font-size: .95rem; padding: .55rem .6rem; border: 1px solid var(--line);
                         border-radius: 10px; background: var(--surface); color: var(--fg); max-width: 14rem; }
.catalog-status { display: flex; gap: 1rem; align-items: center; color: var(--muted); font-size: .9rem; margin-bottom: 1rem; }
.catalog-reset { font: inherit; font-size: .9rem; border: 0; background: none; color: var(--accent); cursor: pointer; padding: 0; }
.catalog-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; }
.catalog-empty { color: var(--muted); padding: 2rem 0; text-align: center; }
.card { position: relative; background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
        padding: 1.1rem 1.2rem; box-shadow: var(--shadow); transition: border-color .15s, transform .15s; }
.card:hover { border-color: var(--accent); transform: translateY(-1px); }
.card > :first-child { margin-top: 0; }
.card > :last-child { margin-bottom: 0; }
.card h2, .card h3 { margin: 0 0 .35rem; font-size: 1.1rem; }
.card h2 a, .card h3 a { color: var(--fg); text-decoration: none; }
.card h2 a::after, .card h3 a::after { content: ""; position: absolute; inset: 0; border-radius: var(--radius); }
.card p { margin: .4rem 0; }
.card img { display: block; margin-bottom: .5rem; }
.card .meta, .meta { color: var(--muted); font-size: .9rem; }
.tag { display: inline-block; background: var(--tag); border-radius: 999px; padding: .1em .65em; font-size: .8rem;
       margin: .15rem .2rem .15rem 0; color: var(--muted); }

/* Detail */
.fields { display: grid; grid-template-columns: max-content 1fr; gap: .5rem 1.5rem; margin: 1.5rem 0;
          background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 1.25rem; }
.fields dt { font-weight: 600; color: var(--muted); }
.fields dd { margin: 0; overflow-wrap: anywhere; }
@media (max-width: 560px) { .fields { grid-template-columns: 1fr; gap: .1rem; } .fields dd { margin-bottom: .6rem; } }
.pager { display: flex; justify-content: space-between; gap: 1rem; margin-top: 2.5rem; padding-top: 1.5rem;
         border-top: 1px solid var(--line); }
.pager a { display: flex; flex-direction: column; text-decoration: none; max-width: 48%; }
.pager a span { font-size: .8rem; color: var(--muted); }
.pager .next { text-align: right; margin-left: auto; }
li.task { list-style: none; }
"""

DEFAULT_JS = r"""(function () {
  "use strict";
  function norm(s) { return (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
  function initCatalog(root) {
    var data = JSON.parse(root.querySelector("script[data-catalog-index]").textContent);
    var sorts = JSON.parse(root.getAttribute("data-sorts") || "[]");
    var list = root.querySelector("[data-list]");
    var cards = Array.prototype.slice.call(list.children);
    var original = cards.slice();
    var search = root.querySelector("[data-search]");
    var filters = Array.prototype.slice.call(root.querySelectorAll("[data-filter]"));
    var sortSel = root.querySelector("[data-sort]");
    var countEl = root.querySelector("[data-count]");
    var emptyEl = root.querySelector("[data-empty]");
    var resetEl = root.querySelector("[data-reset]");
    var params = new URLSearchParams(location.search);
    if (search && params.has("q")) search.value = params.get("q");
    filters.forEach(function (sel) { var k = sel.getAttribute("data-key"); if (params.has(k)) sel.value = params.get(k); });
    if (sortSel && params.has("sort")) sortSel.value = params.get("sort");

    function apply() {
      var terms = norm(search ? search.value : "").split(/\s+/).filter(Boolean);
      var active = filters.filter(function (s) { return s.value !== ""; });
      var shown = 0;
      cards.forEach(function (card) {
        var d = data[+card.getAttribute("data-i")];
        var ok = terms.every(function (t) { return d.s.indexOf(t) !== -1; }) &&
          active.every(function (s) { return d.f[+s.getAttribute("data-filter")].indexOf(s.value) !== -1; });
        card.hidden = !ok;
        if (ok) shown++;
      });
      var order = original.slice();
      var si = sortSel ? sortSel.value : "";
      if (si !== "") {
        var cfg = sorts[+si], k = +si;
        order.sort(function (a, b) {
          var va = data[+a.getAttribute("data-i")].o[k], vb = data[+b.getAttribute("data-i")].o[k];
          var ea = va === null || va === "", eb = vb === null || vb === "";
          if (ea || eb) return ea === eb ? 0 : ea ? 1 : -1;
          var r = cfg.numeric ? va - vb : String(va).localeCompare(String(vb), undefined, { numeric: true });
          return cfg.desc ? -r : r;
        });
      }
      order.forEach(function (c) { list.appendChild(c); });
      countEl.textContent = shown === cards.length ? cards.length + " items" : shown + " of " + cards.length + " items";
      emptyEl.hidden = shown > 0;
      var filtered = (search && search.value) || active.length || si !== "";
      resetEl.hidden = !filtered;
      var p = new URLSearchParams();
      if (search && search.value) p.set("q", search.value);
      active.forEach(function (s) { p.set(s.getAttribute("data-key"), s.value); });
      if (si !== "") p.set("sort", si);
      var qs = p.toString();
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : "") + location.hash);
    }
    if (search) search.addEventListener("input", apply);
    filters.forEach(function (s) { s.addEventListener("change", apply); });
    if (sortSel) sortSel.addEventListener("change", apply);
    resetEl.addEventListener("click", function () {
      if (search) search.value = "";
      filters.forEach(function (s) { s.value = ""; });
      if (sortSel) sortSel.value = "";
      apply();
    });
    apply();
  }
  document.querySelectorAll("[data-catalog]").forEach(initCatalog);
})();
"""


# --------------------------------------------------------------------------
# Starter project written by `init`
# --------------------------------------------------------------------------

STARTER_CONFIG = {
    "title": "Classic Books",
    "description": "A small catalog of public-domain classics you can read for free.",
    "language": "en",
    "source": "data/items.csv",
    "title_column": "A",
    "list_separator": ",",
    "filters": [
        {"column": "D", "label": "Genre"},
        {"column": "E", "label": "Tag", "multiple": True},
        {"column": "F", "label": "Language"},
    ],
    "sort": [
        {"column": "A", "label": "Title (A-Z)"},
        {"column": "C", "label": "Oldest first", "numeric": True},
        {"column": "C", "label": "Newest first", "numeric": True, "descending": True},
    ],
    "nav": [{"label": "About", "url": "about.html"}],
    "footer": "Generated with [catalog-generator](https://github.com/pauginer/catalog-generator) from a spreadsheet.",
    "output": "_site",
}

STARTER_CSV = """Title,Author,Year,Genre,Tags,Language,Summary,Link
Pride and Prejudice,Jane Austen,1813,Novel,"romance, society, family",English,"Elizabeth Bennet trades sharp remarks with the proud Mr Darcy while her family navigates marriage, money and reputation.",https://www.gutenberg.org/ebooks/1342
Moby-Dick,Herman Melville,1851,Novel,"adventure, sea, obsession",English,"Ishmael signs on to a whaling ship whose captain is consumed by his hunt for a white whale.",https://www.gutenberg.org/ebooks/2701
"Frankenstein; or, The Modern Prometheus",Mary Shelley,1818,Novel,"science fiction, horror",English,"A young scientist gives life to a creature and then abandons it, with tragic consequences for both.",https://www.gutenberg.org/ebooks/84
Alice's Adventures in Wonderland,Lewis Carroll,1865,Children's,"fantasy, humour",English,"Alice follows a white rabbit down a hole into a world of riddles, nonsense and peculiar logic.",https://www.gutenberg.org/ebooks/11
The Adventures of Sherlock Holmes,Arthur Conan Doyle,1892,Short stories,"mystery, detective",English,"Twelve cases solved by the famous consulting detective and narrated by his friend Dr Watson.",https://www.gutenberg.org/ebooks/1661
Dracula,Bram Stoker,1897,Novel,"horror, gothic",English,"Told through letters and diaries, a group of friends confront an ancient count who has come to England.",https://www.gutenberg.org/ebooks/345
War and Peace,Leo Tolstoy,1869,Novel,"history, war, family",Russian,"Several aristocratic families live through the Napoleonic invasion of Russia.",https://www.gutenberg.org/ebooks/2600
Don Quixote,Miguel de Cervantes,1605,Novel,"adventure, humour, satire",Spanish,"A country gentleman who has read too many chivalric romances sets out as a knight with his squire Sancho Panza.",https://www.gutenberg.org/ebooks/996
Metamorphosis,Franz Kafka,1915,Novella,"absurdism, family",German,"A travelling salesman wakes up transformed into a giant insect and becomes a burden to his family.",https://www.gutenberg.org/ebooks/5200
The Time Machine,H. G. Wells,1895,Novella,"science fiction, adventure",English,"An inventor travels to the distant future and finds humanity split into two strange species.",https://www.gutenberg.org/ebooks/35
Les Misérables,Victor Hugo,1862,Novel,"history, justice, redemption",French,"The former convict Jean Valjean seeks redemption while being pursued by the relentless inspector Javert.",https://www.gutenberg.org/ebooks/135
Jane Eyre,Charlotte Brontë,1847,Novel,"romance, gothic",English,"An orphaned governess with a strong sense of self falls for her brooding employer, who hides a secret.",https://www.gutenberg.org/ebooks/1260
"""

STARTER_FILES = {
    "templates/index.md": """# $site_title

$description Each entry below comes from one row of the spreadsheet in
`data/items.csv`. Use the search box and filters to narrow the list.

{{catalog}}
""",
    "templates/card.md": """### [$title]($url)

<p class="meta">$B &middot; $C</p>

$G

{{#each E}}<span class="tag">$this</span>{{/each}}
""",
    "templates/item.md": """[&larr; All books]($home)

# $A

<p class="meta">by <strong>$B</strong> &middot; first published $C &middot; ${Language}</p>

$G

{{#if H}}
[Read it for free on Project Gutenberg &rarr;]($H)
{{/if}}

{{#if D == "Novel"}}
> This is one of the **{{count}}** works in the catalog, and it is a full-length novel.
{{else}}
> This is one of the **{{count}}** works in the catalog ($D).
{{/if}}

## Tags

{{#each Tags}}
- $this
{{else}}
No tags yet.
{{/each}}

## All fields

{{fields}}

{{pager}}
""",
    "templates/page.md": DEFAULT_TEMPLATES["page.md"],
    "templates/layout.html": DEFAULT_TEMPLATES["layout.html"],
    "templates/style.css": """/* Your own styles. They are added after the built-in stylesheet,
   so anything here overrides the defaults. For example:

:root { --accent: #b3261e; }
.catalog-list { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
*/
""",
    "pages/about.md": """# About this catalog

This site is generated by **catalog-generator** from a spreadsheet: one row per item.

- The overview page is built from `templates/index.md` and `templates/card.md`.
- Each detail page is built from `templates/item.md`.
- Extra pages like this one live in the `pages/` folder.

The catalog currently lists {{count}} items.
""",
    ".gitignore": "_site/\n__pycache__/\n.DS_Store\n",
    ".github/workflows/deploy.yml": """# Builds the catalog and publishes it with GitHub Pages.
# One-time setup: repository Settings > Pages > Build and deployment > Source: "GitHub Actions".
name: Deploy catalog

on:
  push:
    branches: [main]
  workflow_dispatch:      # adds a "Run workflow" button in the Actions tab
  # Using a Google Sheet? Uncomment to rebuild every day at 06:00 UTC
  # so spreadsheet edits show up without a new commit.
  # schedule:
  #   - cron: "0 6 * * *"

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Build site
        run: python catalog.py build --out _site
        # To keep the sheet URL out of the repo, store it as a secret and uncomment:
        # env:
        #   CATALOG_SOURCE: ${{ secrets.CATALOG_SOURCE }}
      - uses: actions/upload-pages-artifact@v3
        with:
          path: _site

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
""",
}


def init_project(target):
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    files = dict(STARTER_FILES)
    files["catalog.json"] = json.dumps(STARTER_CONFIG, indent=2, ensure_ascii=False) + "\n"
    files["data/items.csv"] = STARTER_CSV
    for rel, content in files.items():
        p = target / rel
        if p.exists():
            log("  skip   %s (already exists)" % rel)
            continue
        write(p, content)
        log("  create %s" % rel)
    me = Path(__file__).resolve()
    dest = target / "catalog.py"
    if dest.resolve() != me:
        if dest.exists():
            log("  skip   catalog.py (already exists)")
        else:
            shutil.copy2(str(me), str(dest))
            log("  create catalog.py")
    rel = os.path.relpath(str(target))
    log("\nDone. Next steps:\n  cd %s\n  python3 catalog.py serve" % rel)


# --------------------------------------------------------------------------
# Local preview server with auto-rebuild
# --------------------------------------------------------------------------

def _snapshot(config_path):
    base = config_path.parent
    paths = [config_path]
    try:
        cfg = load_config(config_path)
    except CatalogError:
        return {str(config_path): config_path.stat().st_mtime if config_path.exists() else 0}
    if not re.match(r"^https?://", cfg["source"]):
        paths.append(base / cfg["source"])
    for d in (cfg["templates_dir"], cfg["pages_dir"], cfg["static_dir"]):
        if (base / d).is_dir():
            paths.extend(p for p in (base / d).rglob("*") if p.is_file())
    snap = {}
    for p in paths:
        try:
            snap[str(p)] = p.stat().st_mtime
        except OSError:
            pass
    return snap


def serve(config_path, port, host, out_override, source_override, force, watch):
    config_path = Path(config_path).resolve()
    out = build(config_path, out_override, source_override, force)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

    handler = functools.partial(Handler, directory=str(out))
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    log("Serving http://%s:%d/  (Ctrl+C to stop)" % ("localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port))

    if watch:
        def watcher():
            last = _snapshot(config_path)
            while True:
                time.sleep(1)
                now = _snapshot(config_path)
                if now != last:
                    last = now
                    log("Change detected, rebuilding...")
                    try:
                        build(config_path, out_override, source_override, True)
                    except CatalogError as e:
                        print("error: %s" % e, file=sys.stderr, flush=True)
        threading.Thread(target=watcher, daemon=True).start()
        log("Watching for changes to the config, data, templates and pages.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nStopped.")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="catalog.py",
        description="Generate a static catalog website from a spreadsheet (CSV or Google Sheets).")
    parser.add_argument("--version", action="version", version="catalog-generator " + VERSION)
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="create a new catalog project with example data and templates")
    p_init.add_argument("folder", nargs="?", default=".", help="where to create the project (default: current folder)")

    for name, helptext in (("build", "generate the site"), ("serve", "build, preview locally and rebuild on changes")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("-c", "--config", default="catalog.json", help="config file (default: catalog.json)")
        p.add_argument("-o", "--out", help="output folder (default: the 'output' setting, usually _site)")
        p.add_argument("-s", "--source", help="override the data source: a CSV path or a Google Sheets URL")
        p.add_argument("--force", action="store_true", help="replace an output folder not created by this tool")
        if name == "serve":
            p.add_argument("-p", "--port", type=int, default=8000)
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--no-watch", action="store_true", help="do not rebuild when files change")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_project(args.folder)
        elif args.command == "build":
            build(args.config, args.out, args.source, args.force)
        elif args.command == "serve":
            serve(args.config, args.port, args.host, args.out, args.source, args.force, not args.no_watch)
        else:
            parser.print_help()
            return 1
    except CatalogError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
