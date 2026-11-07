"""Turn a small YAML or JSON description of a system into a Mermaid data flow diagram, a list of flows that cross
trust boundaries, and a STRIDE-per-element table to fill in. Standard library only (a small YAML subset is
parsed here; PyYAML is used if installed).

Run: python3 dataflow.py <system.yaml|system.json> [--stride] [--out FILE]

Description format (YAML shown, JSON with the same keys works too):

  title: Invoice API
  boundaries:
    - id: internet
      label: Internet (untrusted)
    - id: server
      label: App server
  nodes:
    - id: browser
      label: Customer browser
      kind: actor          # actor | process | store | external
      boundary: internet
    - id: api
      label: HTTP API
      kind: process
      boundary: server
  flows:
    - from: browser
      to: api
      label: HTTPS JSON + bearer token
"""
import json, re, sys
from pathlib import Path

KINDS = {"actor", "process", "store", "external"}
# STRIDE per element (Microsoft SDL): which threat types apply to which element type.
STRIDE = {
    "actor": "S R",
    "external": "S R",
    "process": "S T R I D E",
    "store": "T R I D",
    "flow": "T I D",
}
NAMES = {"S": "Spoofing", "T": "Tampering", "R": "Repudiation", "I": "Information disclosure",
         "D": "Denial of service", "E": "Elevation of privilege"}


def scalar(v):
    v = v.strip()
    if not v:
        return None
    if v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v in {"true", "false"}:
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def mini_yaml(text):
    """Parse the YAML subset this tool needs: nested maps, lists of maps or scalars, inline [a, b], comments."""
    lines = []
    for raw in text.splitlines():
        s = re.sub(r"\s+#.*$", "", raw) if not raw.lstrip().startswith("#") else ""
        if s.strip():
            lines.append((len(s) - len(s.lstrip()), s.strip()))

    def parse(i, indent):
        if lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                rest = lines[i][1][2:].strip()
                if ":" in rest and not rest.startswith(("'", '"')):
                    # list item that is a map: treat "- key: v" as the first line of a map indented by 2
                    lines[i] = (indent + 2, rest)
                    item, i = parse(i, indent + 2)
                    out.append(item)
                else:
                    out.append(scalar(rest))
                    i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
            key, _, val = lines[i][1].partition(":")
            i += 1
            if val.strip():
                out[key.strip()] = scalar(val)
            elif i < len(lines) and lines[i][0] > indent:
                out[key.strip()], i = parse(i, lines[i][0])
            elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                out[key.strip()], i = parse(i, indent)
            else:
                out[key.strip()] = None
        return out, i

    return parse(0, lines[0][0])[0] if lines else {}


def load(path):
    text = Path(path).read_text()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml  # noqa
        return yaml.safe_load(text)
    except ImportError:
        return mini_yaml(text)


def safe_id(s):
    return re.sub(r"\W", "_", str(s))


def esc(s):
    return str(s).replace('"', "'")


def shape(n):
    label = esc(n.get("label", n["id"]))
    k = n.get("kind", "process")
    nid = safe_id(n["id"])
    return {"actor": f'{nid}["{label}"]', "external": f'{nid}[["{label}"]]',
            "process": f'{nid}(("{label}"))', "store": f'{nid}[("{label}")]'}[k]


def validate(spec):
    errors = []
    ids = {n["id"] for n in spec.get("nodes", [])}
    bids = {b["id"] for b in spec.get("boundaries", []) or []}
    for n in spec.get("nodes", []):
        if n.get("kind", "process") not in KINDS:
            errors.append(f"node {n['id']}: kind must be one of {sorted(KINDS)}")
        if n.get("boundary") and n["boundary"] not in bids:
            errors.append(f"node {n['id']}: unknown boundary {n['boundary']}")
    for f in spec.get("flows", []):
        for end in ("from", "to"):
            if f.get(end) not in ids:
                errors.append(f"flow {f.get('from')} -> {f.get('to')}: unknown node {f.get(end)}")
    return errors


def render(spec):
    nodes = {n["id"]: n for n in spec.get("nodes", [])}
    out = ["flowchart LR"]
    for b in spec.get("boundaries", []) or []:
        out.append(f'  subgraph {safe_id(b["id"])}["{esc(b.get("label", b["id"]))}"]')
        for n in nodes.values():
            if n.get("boundary") == b["id"]:
                out.append("    " + shape(n))
        out.append("  end")
    for n in nodes.values():
        if not n.get("boundary"):
            out.append("  " + shape(n))
    crossing = []
    for i, f in enumerate(spec.get("flows", [])):
        a, b = nodes[f["from"]], nodes[f["to"]]
        label = esc(f.get("label", ""))
        crosses = a.get("boundary") != b.get("boundary")
        arrow = f' -- "{i + 1}. {label}" --> ' if label else " --> "
        out.append(f"  {safe_id(a['id'])}{arrow}{safe_id(b['id'])}")
        if crosses:
            crossing.append((i, f))
    for i, _ in crossing:
        out.append(f"  linkStyle {i} stroke:#d33,stroke-width:2px")
    for b in spec.get("boundaries", []) or []:
        out.append(f"  style {safe_id(b['id'])} stroke-dasharray: 5 5")
    return "\n".join(out), crossing


def stride_table(spec, crossing):
    rows = ["| Element | Type | Applies | Threat (fill in, or 'n/a: reason') |", "|---|---|---|---|"]
    for n in spec.get("nodes", []):
        k = n.get("kind", "process")
        for letter in STRIDE[k].split():
            rows.append(f"| {n.get('label', n['id'])} | {k} | {NAMES[letter]} | |")
    for i, f in crossing:
        for letter in STRIDE["flow"].split():
            rows.append(f"| flow {i + 1}: {f['from']} -> {f['to']} | flow (crosses boundary) | {NAMES[letter]} | |")
    return "\n".join(rows)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    out_file = None
    if "--out" in sys.argv:
        out_file = sys.argv[sys.argv.index("--out") + 1]
        args = [a for a in args if a != out_file]
    spec = load(args[0])
    errors = validate(spec)
    if errors:
        print("\n".join("error: " + e for e in errors))
        sys.exit(1)
    diagram, crossing = render(spec)
    parts = ["```mermaid", diagram, "```", "", "Flows that cross a trust boundary (red in the diagram):"]
    for i, f in crossing:
        parts.append(f"- {i + 1}. {f['from']} -> {f['to']}: {f.get('label', '')}")
    if not crossing:
        parts.append("- none (check that boundaries are set; almost every real system has some)")
    if "--stride" in sys.argv:
        parts += ["", stride_table(spec, crossing)]
    text = "\n".join(parts) + "\n"
    if out_file:
        Path(out_file).write_text(text)
        print(f"wrote {out_file}")
    else:
        print(text)


if __name__ == "__main__":
    main()
