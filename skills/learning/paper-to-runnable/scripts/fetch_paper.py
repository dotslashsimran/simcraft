#!/usr/bin/env python3
"""Fetch a paper and turn it into text you can read and equations you can cite.

Usage:
  python3 fetch_paper.py 2201.02177                 # arXiv id
  python3 fetch_paper.py https://arxiv.org/abs/1706.03762
  python3 fetch_paper.py path/to/paper.pdf          # local PDF, no network
  python3 fetch_paper.py <id> --out papers/my-name  # choose the output folder

Writes into the output folder (default ./papers/<id>):
  meta.json        title, authors, abstract, links (arXiv only)
  paper.pdf        the PDF
  paper.txt        plain text of the PDF
  source/          the LaTeX files from the arXiv source bundle, if there is one
  equations.md     every display equation in the LaTeX, with its label, file and
                   line, plus the author's \\newcommand macros so symbols can be read

Only arXiv is downloaded from. Any other URL is refused: ask the user, download
it yourself after they agree, and pass the local PDF path instead.

Uses only the standard library. Text extraction tries `pdftotext` first, then
the pypdf package, then a rough built-in reader.
"""
import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib

UA = "paper-to-runnable/1.0 (personal research script; polite single downloads)"
PAUSE = 3.0  # arXiv asks for no more than one request every 3 seconds

NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
OLD_ID = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
ID_RE = re.compile(rf"^(?:arxiv:)?({NEW_ID}|{OLD_ID})$", re.I)
URL_RE = re.compile(rf"arxiv\.org/(?:abs|pdf|e-print|src)/({NEW_ID}|{OLD_ID})", re.I)

_last = [0.0]


def get(url):
    wait = PAUSE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    finally:
        _last[0] = time.time()


def parse_target(s):
    s = s.strip()
    if os.path.isfile(s):
        return "file", s
    m = ID_RE.match(s) or URL_RE.search(s)
    if m:
        return "arxiv", re.sub(r"\.pdf$", "", m.group(1))
    if s.startswith("http"):
        sys.exit("refused: not an arXiv link. Ask the user before downloading from "
                 "anywhere else, then pass the local PDF path.")
    sys.exit(f"not a file, arXiv id or arXiv URL: {s}")


def fetch_meta(aid):
    xml = get(f"https://export.arxiv.org/api/query?id_list={aid}")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    e = ET.fromstring(xml).find("a:entry", ns)
    if e is None or e.find("a:title", ns) is None:
        return {"id": aid}
    clean = lambda t: re.sub(r"\s+", " ", (t or "").strip())
    return {
        "id": aid,
        "title": clean(e.findtext("a:title", "", ns)),
        "authors": [clean(a.findtext("a:name", "", ns)) for a in e.findall("a:author", ns)],
        "published": e.findtext("a:published", "", ns),
        "abstract": clean(e.findtext("a:summary", "", ns)),
        "abs_url": f"https://arxiv.org/abs/{aid}",
        "pdf_url": f"https://arxiv.org/pdf/{aid}",
    }


# ---------- PDF to text ----------

def pdf_text(pdf, txt):
    exe = shutil.which("pdftotext")
    if exe:
        subprocess.run([exe, "-layout", pdf, txt], check=True)
        return "pdftotext"
    try:
        from pypdf import PdfReader
        pages = [p.extract_text() or "" for p in PdfReader(pdf).pages]
        open(txt, "w").write("\n\f".join(pages))
        return "pypdf"
    except ImportError:
        pass
    # this skill's own env (made by setup.sh) has pypdf even when this python does not
    venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".venv", "bin", "python")
    if os.path.exists(venv_py) and os.path.abspath(sys.executable) != os.path.abspath(venv_py):
        code = ("import sys; from pypdf import PdfReader; "
                "open(sys.argv[2], 'w').write('\\n\\f'.join(p.extract_text() or '' "
                "for p in PdfReader(sys.argv[1]).pages))")
        if subprocess.run([venv_py, "-I", "-c", code, pdf, txt]).returncode == 0:
            return "pypdf (skill .venv)"
    open(txt, "w").write(rough_pdf_text(open(pdf, "rb").read()))
    return "built-in rough reader (run setup.sh or install poppler for clean text)"


def rough_pdf_text(data):
    """Last resort: inflate every stream and pull out the strings shown with Tj/TJ.
    Good enough to find section titles and numbers, not to read math."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            s = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        for t in re.finditer(rb"\[(.*?)\]\s*TJ|\((.*?)\)\s*Tj", s, re.S):
            chunk = t.group(1) or t.group(2)
            parts = re.findall(rb"\(((?:\\.|[^\\)])*)\)", chunk) if t.group(1) else [chunk]
            out.append(b"".join(parts).decode("latin-1", "replace"))
        out.append("\n")
    return re.sub(r"\n{3,}", "\n\n", " ".join(out))


# ---------- LaTeX source ----------

def save_source(blob, dest):
    """The e-print is a gzipped tar, a single gzipped .tex, or a PDF. Keep text files only."""
    os.makedirs(dest, exist_ok=True)
    keep = (".tex", ".bbl", ".bib", ".sty", ".cls")
    try:
        raw = gzip.decompress(blob)
    except OSError:
        raw = blob
    if raw[:4] == b"%PDF":
        return []
    saved = []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for m in tar.getmembers():
                name = os.path.normpath(m.name)
                if not m.isfile() or name.startswith("..") or os.path.isabs(name):
                    continue  # skip links and anything that escapes the folder
                if not name.lower().endswith(keep) or m.size > 5_000_000:
                    continue
                path = os.path.join(dest, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(tar.extractfile(m).read())
                saved.append(name)
    except tarfile.ReadError:
        open(os.path.join(dest, "main.tex"), "wb").write(raw)
        saved.append("main.tex")
    return saved


ENVS = r"equation|align|eqnarray|gather|multline|flalign|alignat"
ENV_RE = re.compile(rf"\\begin\{{({ENVS})(\*?)\}}(.*?)\\end\{{\1\*?\}}", re.S)
DISPLAY_RE = re.compile(r"\\\[(.*?)\\\]|\$\$(.*?)\$\$", re.S)
MACRO_RE = re.compile(r"\\(?:re)?newcommand\*?\{?\\\w+\}?(?:\[\d\])?\{.*?\}\s*$|\\DeclareMathOperator\*?\{\\\w+\}\{.*?\}", re.M)


def strip_comments(tex):
    return re.sub(r"(?<!\\)%.*", "", tex)


def tex_files_in_order(src):
    """Follow \\input/\\include from the file with \\documentclass, so numbering
    follows the paper. Files never reached are appended at the end."""
    files = {}
    for root, _, names in os.walk(src):
        for n in names:
            if n.endswith(".tex"):
                p = os.path.join(root, n)
                files[os.path.relpath(p, src)] = strip_comments(open(p, errors="replace").read())
    mains = [f for f, t in files.items() if "\\documentclass" in t]
    order, seen = [], set()

    def visit(f):
        if f in seen or f not in files:
            return
        seen.add(f)
        order.append(f)
        for m in re.finditer(r"\\(?:input|include|subfile)\{([^}]+)\}", files[f]):
            name = m.group(1).strip()
            visit(name if name.endswith(".tex") else name + ".tex")

    for f in mains:
        visit(f)
    order += [f for f in sorted(files) if f not in seen]
    return order, files


def extract_equations(src, out_md):
    order, files = tex_files_in_order(src)
    rows, macros, n = [], [], 0
    for f in order:
        tex = files[f]
        macros += [m.group(0).strip() for m in MACRO_RE.finditer(tex)]
        found = []
        for m in ENV_RE.finditer(tex):
            found.append((m.start(), m.group(1), m.group(2) == "*", m.group(3)))
        for m in DISPLAY_RE.finditer(tex):
            found.append((m.start(), "display", True, m.group(1) or m.group(2)))
        for pos, env, starred, body in sorted(found):
            line = tex.count("\n", 0, pos) + 1
            labels = re.findall(r"\\label\{([^}]+)\}", body)
            num = ""
            if not starred:
                # multi-line environments number every line unless told not to
                lines = re.split(r"\\\\", body) if env != "equation" else [body]
                k = sum(1 for l in lines if l.strip() and not re.search(r"\\nonumber|\\notag", l))
                if k:
                    num = f"({n + 1})" if k == 1 else f"({n + 1})-({n + k})"
                    n += k
            rows.append((num, f, line, ", ".join(labels), body.strip()))
    with open(out_md, "w") as w:
        w.write("# Equations from the LaTeX source\n\n")
        w.write("Numbers are counted from the source in reading order. They usually match the "
                "PDF, but check against paper.txt before citing one (appendix resets, "
                "\\tag and custom counters can shift them).\n\n")
        if macros:
            w.write("## Author macros\n\n```latex\n" + "\n".join(dict.fromkeys(macros)) + "\n```\n\n")
        w.write("## Display equations\n\n")
        for num, f, line, labels, body in rows:
            head = f"**{num or 'unnumbered'}**  `{f}:{line}`" + (f"  label: `{labels}`" if labels else "")
            w.write(f"{head}\n\n```latex\n{body}\n```\n\n")
    return len(rows), n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="arXiv id, arXiv URL, or local PDF path")
    ap.add_argument("--out", help="output folder (default ./papers/<id>)")
    ap.add_argument("--no-source", action="store_true", help="skip the LaTeX source bundle")
    a = ap.parse_args()

    kind, ref = parse_target(a.target)
    name = os.path.splitext(os.path.basename(ref))[0] if kind == "file" else ref.replace("/", "_")
    out = a.out or os.path.join("papers", name)
    os.makedirs(out, exist_ok=True)
    pdf = os.path.join(out, "paper.pdf")

    if kind == "file":
        if os.path.abspath(ref) != os.path.abspath(pdf):
            shutil.copyfile(ref, pdf)
    else:
        meta = fetch_meta(ref)
        json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=2)
        print(f"title:   {meta.get('title', '?')}")
        print(f"authors: {', '.join(meta.get('authors', [])[:6])}")
        open(pdf, "wb").write(get(f"https://arxiv.org/pdf/{ref}"))

    how = pdf_text(pdf, os.path.join(out, "paper.txt"))
    words = len(open(os.path.join(out, "paper.txt"), errors="replace").read().split())
    print(f"text:    paper.txt ({words} words, via {how})")

    if kind == "arxiv" and not a.no_source:
        try:
            saved = save_source(get(f"https://arxiv.org/e-print/{ref}"), os.path.join(out, "source"))
        except Exception as e:  # source is a bonus, never fatal
            saved = []
            print(f"source:  not available ({e})")
        numbered = 0
        if saved:
            total, numbered = extract_equations(os.path.join(out, "source"), os.path.join(out, "equations.md"))
            print(f"source:  {len(saved)} files; equations.md has {total} display equations, {numbered} numbered")
        else:
            print("source:  no LaTeX (PDF-only submission); read equations from paper.txt")
        if saved and numbered == 0:
            print("note:    no numbered equations; cite sections, figures and algorithms instead")
    print(f"folder:  {out}")


if __name__ == "__main__":
    main()
