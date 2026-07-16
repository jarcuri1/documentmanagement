"""
LeaseFolders bootstrap — draft the address->folder map for lease filing
=======================================================================
Step 1 of the signed-lease filing feature (SPEC_LEASE_FILING.md). Walks Jay's
two property trees ON THE PC (local paths, so it handles the nested Dropbox
mounts the API can't) and writes a DRAFT mapping to
`C:\\AIAgents\\shared\\lease_folders.json` for Jay to review/correct ONCE.

Why a mapping and not inference: the trees are too inconsistent for
address->folder guessing (a wrong guess files a lease in the wrong owner's
folder). This script does ~90% mechanically and flags everything uncertain in
a `_review` block so the human pass is quick.

WHAT IT EMITS (draft — Jay edits, then deletes the `_review` block):
{
  "61-cliff-st-naugatuck": {
    "folder": "D:\\Dropbox\\Dropbox\\Personal Properties\\61 Cliff St Naugatuck",
    "tree": "personal",
    "owner": null,
    "units": { "first floor": "61 Cliff First Floor",
               "second floor": "61 Cliff Second Floor",
               "third floor": "61 Cliff Third Floor" }
  },
  ...
  "_review": [ { "folder": "...", "why": "ambiguous — property or owner container?" } ]
}

The mapping KEY is a slug of the property. It must match the property-part of
the job slug the fill agent produces from the intake `property` field, so on
review Jay aligns keys with how he actually names properties in intake. Keys
are a starting point, not gospel.

RUN (on the fleet PC):
  python lease_folders_bootstrap.py                 # writes the draft
  python lease_folders_bootstrap.py --print         # print, don't write
  python lease_folders_bootstrap.py --out some.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

PERSONAL_ROOT = os.environ.get("LEASE_PERSONAL_ROOT", r"D:\Dropbox\Dropbox\Personal Properties")
PREMIO_ROOT = os.environ.get("LEASE_PREMIO_ROOT", r"D:\Dropbox\Dropbox\Premio Property Managment")
OUT_DEFAULT = os.environ.get("LEASE_FOLDERS_JSON", r"C:\AIAgents\shared\lease_folders.json")

# Folder names that are never a property or a filing destination.
_SKIP_EXACT = {
    "past tenants", "closing docs", "listing photos", "move-in checklist",
    "leases", "pictures", "tenant list docs", "listing info",
}
_SKIP_CONTAINS = ("listing info", "listing photos", "appraisal", "receipts")
_SKIP_SUBTREES = {"sold properties"}   # do not descend at all

# A folder whose name ends with one of these holds properties, it is not one.
_CONTAINER_SUFFIXES = (" llc", " investors", " realty", " enterprises")

# Unit-subfolder signals (a child of a property, not a property itself).
_UNIT_RE = re.compile(
    r"(#|\bunit\b|\bapt\b|\bfl\b|\bfloor\b|\bfront\b|\bback\b|\bleft\b|\bright\b|"
    r"\b\d+[a-z]\b|\b[123][nsew]\b|first fl|second fl|third fl)",
    re.IGNORECASE,
)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def is_skip(name):
    n = name.strip().lower()
    return n in _SKIP_EXACT or any(s in n for s in _SKIP_CONTAINS)


def is_container(name):
    return name.strip().lower().endswith(_CONTAINER_SUFFIXES)


def looks_like_address(name):
    # A street address folder starts with a street number.
    return bool(re.match(r"^\s*\d", name))


def looks_like_unit(name):
    return bool(_UNIT_RE.search(name))


def _subdirs(path):
    try:
        return sorted([e for e in os.scandir(path) if e.is_dir()], key=lambda e: e.name)
    except OSError:
        return []


def scan_property(prop_path, tree, owner, mapping, review):
    """Record one property folder plus any unit subfolders it contains."""
    name = os.path.basename(prop_path.rstrip("\\/"))
    key = slugify(name)
    units = {}
    for child in _subdirs(prop_path):
        cn = child.name
        if is_skip(cn):
            continue
        if looks_like_unit(cn):
            # Map a normalized unit label -> the actual subfolder name.
            units[cn.lower()] = cn
    entry = {"folder": prop_path, "tree": tree, "owner": owner}
    if units:
        entry["units"] = units
    if key in mapping:
        review.append({"folder": prop_path, "why": f"duplicate slug {key!r} (also {mapping[key]['folder']})"})
        key = f"{key}--{slugify(owner) if owner else tree}"
    mapping[key] = entry


def walk_personal(root, mapping, review):
    for top in _subdirs(root):
        n = top.name
        if n.strip().lower() in _SKIP_SUBTREES or is_skip(n):
            continue
        if is_container(n):
            # LLC/owner container: its address-like children are properties.
            for prop in _subdirs(top.path):
                if is_skip(prop.name):
                    continue
                if looks_like_address(prop.name):
                    scan_property(prop.path, "personal", n, mapping, review)
                else:
                    review.append({"folder": prop.path, "why": f"inside container {n!r} but name is not address-like"})
        elif looks_like_address(n):
            scan_property(top.path, "personal", None, mapping, review)
        else:
            review.append({"folder": top.path, "why": "top-level, not address-like and not a known container"})


def walk_premio(root, mapping, review):
    # Premio is inconsistent: owner folders containing property folders, single
    # folders mixing property+owner, and cryptic names. Heuristic: if a folder
    # holds address-like subfolders, treat it as an owner container; otherwise
    # it is a property (leases live loose inside). Everything here is flagged
    # lower-confidence for review because the tree is irregular.
    for top in _subdirs(root):
        n = top.name
        if is_skip(n):
            continue
        children = [c for c in _subdirs(top.path) if not is_skip(c.name)]
        prop_children = [c for c in children if looks_like_address(c.name) and not looks_like_unit(c.name)]
        if prop_children:
            for prop in prop_children:
                scan_property(prop.path, "premio", n, mapping, review)
                review.append({"folder": prop.path, "why": f"premio: assumed property under owner {n!r} — confirm"})
        elif looks_like_address(n):
            scan_property(top.path, "premio", None, mapping, review)
            review.append({"folder": top.path, "why": "premio: assumed single property (leases loose) — confirm"})
        else:
            # Not address-like and no property children -> almost certainly a
            # docs folder (Insurance, W9, ...). Note it, don't map it.
            review.append({"folder": top.path, "why": "premio: not address-like — likely not a property, left unmapped"})


def build(personal_root, premio_root):
    mapping, review = {}, []
    if os.path.isdir(personal_root):
        walk_personal(personal_root, mapping, review)
    else:
        review.append({"folder": personal_root, "why": "personal root not found on this PC"})
    if os.path.isdir(premio_root):
        walk_premio(premio_root, mapping, review)
    else:
        review.append({"folder": premio_root, "why": "premio root not found on this PC"})
    out = dict(sorted(mapping.items()))
    out["_review"] = review
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--print", action="store_true", dest="just_print")
    args = ap.parse_args()
    result = build(PERSONAL_ROOT, PREMIO_ROOT)
    n_props = sum(1 for k in result if k != "_review")
    text = json.dumps(result, indent=2)
    if args.just_print:
        print(text)
    else:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {n_props} properties to {args.out} "
              f"({len(result['_review'])} flagged for review).")
        print("Review the _review block, correct keys/units, then delete _review.")
