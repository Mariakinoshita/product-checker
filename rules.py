"""
The "brain" of the Product Checker.

Everything in here works on plain Python data, with no Shopify calls, so it can
be tested on its own. It decides:
  * what kind of product something is (from its title)
  * which product info is required for that kind of product
  * what to suggest for missing info (type, category, HS code, weight, SKU...)
  * exactly which changes would be written to Shopify
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(__file__).parent / "config"

# ---------------------------------------------------------------------------
# Loading the config files
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    keywords: list[str]
    product_type: str
    category: str
    hs_code: str
    weight_kg: float | None
    group: str  # music / digital / clothing / bundle / other
    sku_code: str
    needs_size_guide: bool
    usual_countries: list[str] = field(default_factory=list)  # e.g. ["HN", "BD", "IN", "CN", "PK"]


def _to_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_rules(path: Path | str = CONFIG_DIR / "product_rules.csv") -> list[Rule]:
    rules = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            keywords = [k.strip().lower() for k in (row.get("keywords") or "").split("|") if k.strip()]
            if not keywords:
                continue
            rules.append(
                Rule(
                    keywords=keywords,
                    product_type=(row.get("product_type") or "").strip(),
                    category=(row.get("category") or "").strip(),
                    hs_code=re.sub(r"\D", "", row.get("hs_code") or ""),
                    weight_kg=_to_float(row.get("weight_kg")),
                    group=(row.get("group") or "other").strip().lower() or "other",
                    sku_code=(row.get("sku_code") or "").strip().upper(),
                    needs_size_guide=(row.get("needs_size_guide") or "").strip().lower() in ("yes", "y", "true", "1"),
                    usual_countries=[c.strip().upper() for c in (row.get("usual_countries") or "").split("|") if c.strip()],
                )
            )
    return rules


@dataclass
class Blank:
    """A garment blank and its size guide, e.g. AS Colour 5026."""

    name: str
    sizes: list[str]
    rows: list[tuple[str, dict[str, str]]]  # [(measurement, {size: value})]
    weight_kg: float | None = None

    def as_text(self) -> str:
        lines = ["\t".join(["Measurement"] + self.sizes)]
        for measurement, values in self.rows:
            lines.append("\t".join([measurement] + [values.get(s, "") for s in self.sizes]))
        return "\n".join(lines)

    def as_html(self) -> str:
        head = "".join(f"<th>{s}</th>" for s in ["Measurement"] + self.sizes)
        body = "".join(
            "<tr>" + f"<td>{m}</td>" + "".join(f"<td>{v.get(s, '')}</td>" for s in self.sizes) + "</tr>"
            for m, v in self.rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    def as_rich_text(self) -> str:
        """Shopify rich text fields can't hold tables, so it's one line per row."""
        paragraphs = [
            {"type": "paragraph", "children": [{"type": "text", "value": line.replace("\t", "   ")}]}
            for line in self.as_text().splitlines()
        ]
        return json.dumps({"type": "root", "children": paragraphs})

    def as_json(self) -> str:
        return json.dumps({"blank": self.name, "sizes": self.sizes,
                           "rows": [{"measurement": m, **v} for m, v in self.rows]})

    def render(self, fmt: str, field_type: str = "") -> str:
        if fmt in ("", "auto"):
            fmt = {"rich_text_field": "rich_text", "json": "json"}.get(field_type, "html")
        return {"text": self.as_text, "rich_text": self.as_rich_text, "json": self.as_json}.get(fmt, self.as_html)()


def load_blanks(path: Path | str = CONFIG_DIR / "size_guides.csv") -> list[Blank]:
    """size_guides.csv: one row per blank + measurement, one column per size."""
    blanks: dict[str, Blank] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        size_cols = [c for c in reader.fieldnames or [] if c not in ("blank", "measurement", "weight_kg")]
        for row in reader:
            name = (row.get("blank") or "").strip()
            if not name:
                continue
            blank = blanks.setdefault(name, Blank(name, [], []))
            if _to_float(row.get("weight_kg")):
                blank.weight_kg = _to_float(row.get("weight_kg"))
            values = {s: (row.get(s) or "").strip() for s in size_cols if (row.get(s) or "").strip()}
            blank.rows.append(((row.get("measurement") or "").strip(), values))
    for blank in blanks.values():
        used = {s for _, v in blank.rows for s in v}
        blank.sizes = [s for s in size_cols if s in used]
    return list(blanks.values())


# ---------------------------------------------------------------------------
# Finding release date / tracklist / size guide fields without being told
# ---------------------------------------------------------------------------

METAFIELD_WORDS = {"release_date": [["release"]], "tracklist": [["track"]],
                   # a size guide field must say "size" AND "guide"/"chart", so Shopify's own "Size" field
                   # (the S/M/L attribute on every clothing product) is never mistaken for one
                   "size_guide": [["size", "guide"], ["size", "chart"]]}


def _matches(alias: str, namespace: str, key: str, settings: dict) -> bool:
    conf = settings.get("metafields", {}).get(alias, {})
    if conf.get("key"):
        return key == conf["key"] and (not conf.get("namespace") or namespace == conf["namespace"])
    if (namespace or "").lower() == "shopify":  # Shopify's built-in category fields (size, colour, fabric…)
        return False
    k = key.lower()
    return any(all(w in k for w in words) for words in METAFIELD_WORDS[alias])


def pick_metafields(all_metafields: list[dict], settings: dict) -> dict:
    """From every metafield on a product, picks out release date / tracklist / size guide.
    all_metafields: [{namespace, key, type, value}]"""
    out = {alias: None for alias in METAFIELD_WORDS}
    for alias in METAFIELD_WORDS:
        for m in all_metafields:
            if _matches(alias, m.get("namespace", ""), m.get("key", ""), settings) and not _blank(m.get("value")):
                out[alias] = m["value"]
                break
    return out


def metafield_label(m: dict) -> str:
    """Readable value of one metafield: the names of linked pages/entries, or the start of the text."""
    refs = [m.get("reference")] + list(((m.get("references") or {}).get("nodes")) or [])
    names = [r.get("title") or r.get("displayName") for r in refs if r]
    names = [n for n in names if n]
    if names:
        return ", ".join(names)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(m.get("value") or ""))).strip()[:60]


def describe_size_guide(all_metafields: list[dict], settings: dict, field: dict | None = None) -> str:
    """What the 'Size guide now' column shows.
    Looks at the field the app saves to; if that's empty but another size field is filled,
    says so, so you can spot when your store uses a different field."""
    filled = [m for m in all_metafields
              if _matches("size_guide", m.get("namespace", ""), m.get("key", ""), settings) and not _blank(m.get("value"))]
    if field and field.get("key"):
        here = [m for m in all_metafields if m.get("namespace") == field.get("namespace")
                and m.get("key") == field.get("key") and not _blank(m.get("value"))]
        if here:
            return metafield_label(here[0])
        if filled:
            o = filled[0]
            return f"None in {field['namespace']}.{field['key']}, but {o['namespace']}.{o['key']} has: {metafield_label(o)}"
        return "No size guide"
    return metafield_label(filled[0]) if filled else "No size guide"


def size_guide_label(all_metafields: list[dict], settings: dict) -> str:
    return describe_size_guide(all_metafields, settings)


PAGE_FIELD_TYPES = ("page_reference", "list.page_reference")


def page_link_value(page_id: str, field_type: str) -> str:
    """What to save in a field that links to a page: the page id, or a list of one for list fields."""
    return json.dumps([page_id]) if field_type.startswith("list.") else page_id


def match_page_for_blank(blank_name: str, pages: list[dict]) -> dict | None:
    """Finds the size guide page for a blank by its style number, e.g. '5026' in 'AS Colour 5026'."""
    numbers = re.findall(r"\d{3,}", blank_name)
    for page in pages:
        if numbers and all(n in page["title"] for n in numbers):
            return page
    return None


def pick_definition(definitions: list[dict], alias: str, settings: dict) -> dict | None:
    """Which store field to SAVE the size guide into. Prefers keys with 'guide' or 'chart' in them."""
    candidates = [d for d in definitions if _matches(alias, d["namespace"], d["key"], settings)]
    candidates.sort(key=lambda d: (0 if ("guide" in d["key"] or "chart" in d["key"]) else 1))
    return candidates[0] if candidates else None


def load_settings(path: Path | str = CONFIG_DIR / "settings.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_stores(path: Path | str = CONFIG_DIR / "stores.csv") -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [
            {k: (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(f)
            if (row.get("store_name") or "").strip()
        ]


# ---------------------------------------------------------------------------
# Working out what a product is
# ---------------------------------------------------------------------------


def _keyword_in(keyword: str, text: str) -> bool:
    """True if keyword appears in text as a whole word.

    "lp" matches "3LP" and "LP" but not "help"; "pin" does not match "Pink";
    "tee" does not match "teenage".
    """
    pattern = r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])"
    return re.search(pattern, text.lower()) is not None


def match_rule(title: str, rules: list[Rule], current_type: str = "") -> Rule | None:
    """First rule whose keyword is in the title wins (so order in the CSV matters).

    If nothing in the title matches, fall back to the product's current type.
    """
    for rule in rules:
        if any(_keyword_in(k, title) for k in rule.keywords):
            return rule
    if current_type:
        for rule in rules:
            if rule.product_type.lower() == current_type.strip().lower():
                return rule
    return None


def is_preorder(product: dict, settings: dict) -> bool:
    wanted = {t.lower() for t in settings.get("preorder_tags", [])}
    return any(t.strip().lower() in wanted for t in product.get("tags", []))


# What each kind of product must have, on top of the checks every product gets.
BASE_CHECKS = ["title", "image", "type", "category", "tags", "price", "cost", "sku"]
GROUP_CHECKS = {
    "music": ["barcode", "weight", "country", "hs_code", "release_date", "tracklist"],
    "digital": ["release_date", "tracklist"],
    "clothing": ["weight", "country", "hs_code"],
    "bundle": ["weight", "country"],
    "other": ["weight", "country", "hs_code"],
}
CHECK_LABELS = {
    "title": "Title",
    "image": "Image",
    "type": "Type",
    "category": "Category",
    "tags": "Tags",
    "price": "Price",
    "cost": "Cost",
    "sku": "SKU",
    "barcode": "Barcode",
    "weight": "Weight",
    "country": "Country of origin",
    "hs_code": "HS code",
    "release_date": "Release date",
    "tracklist": "Tracklist",
    "size_guide": "Size guide",
}


def required_checks(product: dict, rule: Rule | None, settings: dict) -> list[str]:
    group = rule.group if rule else "other"
    checks = BASE_CHECKS + GROUP_CHECKS.get(group, GROUP_CHECKS["other"])
    if rule and rule.needs_size_guide:
        checks = checks + ["size_guide"]
    # Anything (not just clothing) on preorder needs some kind of release date.
    if is_preorder(product, settings) and "release_date" not in checks:
        checks = checks + ["release_date"]
    return checks


# ---------------------------------------------------------------------------
# The audit: what's there and what's missing
# ---------------------------------------------------------------------------

TRACKLIST_MARKERS = ["tracklist", "track list", "tracklisting", "side a", "side 1", "disc 1", "cd 1", "a1"]


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


def has_tracklist(product: dict, settings: dict) -> bool:
    if not _blank(product.get("metafields", {}).get("tracklist")):
        return True
    if settings.get("tracklist_in_description_counts", True):
        desc = re.sub(r"<[^>]+>", " ", product.get("description") or "").lower()
        return any(m in desc for m in TRACKLIST_MARKERS)
    return False


def has_release_date(product: dict, settings: dict | None = None) -> bool:
    """A release date field is filled in, or the description mentions one
    ("Release date: 23 Jan", "Ships late March", "Expected dispatch…")."""
    if not _blank(product.get("metafields", {}).get("release_date")):
        return True
    words = (settings or {}).get("release_date_words_in_description", [])
    desc = re.sub(r"<[^>]+>", " ", product.get("description") or "").lower()
    return any(w.lower() in desc for w in words)


def check_product(product: dict, rule: Rule | None, settings: dict) -> dict[str, str]:
    """Returns {check_name: "ok" | "missing" | "duplicate"} for each required check."""
    variants = product.get("variants", [])
    results = {}

    def all_variants(test) -> bool:
        return bool(variants) and all(test(v) for v in variants)

    for check in required_checks(product, rule, settings):
        if check == "title":
            ok = not _blank(product.get("title"))
        elif check == "image":
            ok = bool(product.get("has_image"))
        elif check == "type":
            ok = not _blank(product.get("product_type"))
        elif check == "category":
            ok = not _blank(product.get("category_id"))
        elif check == "tags":
            ok = len([t for t in product.get("tags", []) if t.strip()]) > 0
        elif check == "price":
            ok = all_variants(lambda v: (_to_float(v.get("price")) or 0) > 0)
        elif check == "cost":
            ok = all_variants(lambda v: _to_float(v.get("cost")) is not None)
        elif check == "sku":
            ok = all_variants(lambda v: not _blank(v.get("sku")))
        elif check == "barcode":
            ok = all_variants(lambda v: not _blank(v.get("barcode")))
        elif check == "weight":
            ok = all_variants(lambda v: (_to_float(v.get("weight_kg")) or 0) > 0)
        elif check == "country":
            ok = all_variants(lambda v: not _blank(v.get("country")))
        elif check == "hs_code":
            ok = all_variants(lambda v: not _blank(v.get("hs_code")))
        elif check == "release_date":
            ok = has_release_date(product, settings)
        elif check == "tracklist":
            ok = has_tracklist(product, settings)
        elif check == "size_guide":
            ok = not _blank(product.get("metafields", {}).get("size_guide"))
        else:
            continue
        results[check] = "ok" if ok else "missing"
    return results


def find_duplicate_skus(variant_skus: dict[str, str], store_skus: dict[str, str] | None = None) -> set[str]:
    """variant_skus: {variant_id: sku} for the products being checked.
    store_skus: {variant_id: sku} for the rest of the store (optional).
    Returns the set of variant ids whose SKU is used more than once."""
    seen: dict[str, list[str]] = {}
    for vid, sku in {**(store_skus or {}), **variant_skus}.items():
        if _blank(sku):
            continue
        seen.setdefault(sku.strip().upper(), []).append(vid)
    dupes = set()
    for vids in seen.values():
        if len(vids) > 1:
            dupes.update(v for v in vids if v in variant_skus)
    return dupes


# ---------------------------------------------------------------------------
# SKU building
# ---------------------------------------------------------------------------


def size_code(value: str, settings: dict) -> str | None:
    codes = settings.get("size_codes", {})
    key = (value or "").strip().lower()
    if key in codes:
        return codes[key]
    # Already a code like "S" / "XL" / "2XL"
    if re.fullmatch(r"(x{0,4}[sml]|\d?xl|\d?xs|os)", key):
        return key.upper()
    return None


def _option_kind(name: str) -> str:
    n = name.lower()
    if "size" in n:
        return "size"
    if "colour" in n or "color" in n:
        return "colour"
    return "other"


def option_kinds(product: dict, settings: dict) -> dict[str, str]:
    """Works out which option is size / colour / other.
    An option not called "Size" still counts as size if all its values are sizes."""
    kinds = {}
    values_by_option: dict[str, set[str]] = {}
    for v in product.get("variants", []):
        for name, value in (v.get("options") or {}).items():
            values_by_option.setdefault(name, set()).add(value)
    for name, values in values_by_option.items():
        kind = _option_kind(name)
        if kind == "other" and values and all(size_code(x, settings) for x in values):
            kind = "size"
        if name.lower() == "title" and values <= {"Default Title"}:
            kind = "default"
        kinds[name] = kind
    return kinds


def find_colour(text: str, settings: dict, ignore: str = "") -> str:
    """Finds the colour in a title. Uses the LAST colour word, because the colour
    usually sits right before the product word ("Pink Floyd Grey Tee" -> GREY).
    `ignore` is the artist name, so "Pink Floyd" / "Deep Purple" aren't read as colours."""
    text_l = (text or "").lower()
    if ignore:
        text_l = text_l.replace(ignore.lower(), " ")
    found = []
    for colour in settings.get("colours", []):
        for m in re.finditer(r"(?<![a-z])" + re.escape(colour) + r"(?![a-z])", text_l):
            found.append((m.start(), colour))
    if not found:
        return ""
    colour = sorted(found)[-1][1]
    return "GREY" if colour == "gray" else colour.upper()


def _short_code(value: str, max_len: int = 6) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()[:max_len]


def sku_numbers_in_use(skus, artist_code: str) -> set[int]:
    """Finds the numbers already used, e.g. PF001-..., PF014-... -> {1, 14}."""
    pattern = re.compile(r"^" + re.escape(artist_code.upper()) + r"(\d{3,})(?:-|$)")
    used = set()
    for sku in skus:
        m = pattern.match((sku or "").strip().upper())
        if m:
            used.add(int(m.group(1)))
    return used


def product_sku_number(product: dict, artist_code: str) -> int | None:
    """If the product already has an ARTIST### SKU, reuse that number."""
    used = sku_numbers_in_use([v.get("sku") for v in product.get("variants", [])], artist_code)
    return min(used) if used else None


def sku_description(product: dict, rule: Rule | None, settings: dict, artist_name: str = "") -> str:
    """The middle part of an auto SKU: item code + colour, e.g. 'TSGREY' (colour left off if it's a variant option)."""
    return auto_base_sku(product, rule, "X", 0, settings, artist_name).split("-", 1)[1]


def compose_base_sku(artist_code: str, number, description: str) -> str:
    """('QUE', '7', 'TSGREY') -> 'QUE007-TSGREY'. Returns '' if anything is missing or the number isn't a number."""
    num = str(number or "").strip()
    if not artist_code or not description or not num.isdigit():
        return ""
    return f"{artist_code.upper()}{int(num):03d}-{description.strip().upper()}"


def auto_base_sku(
    product: dict, rule: Rule | None, artist_code: str, number: int, settings: dict, artist_name: str = ""
) -> str:
    """e.g. Pink Floyd grey tee, number 1 -> PF001-TSGREY.
    If the product has a colour option, the colour is added per variant instead."""
    code = rule.sku_code if rule and rule.sku_code else "ITEM"
    kinds = option_kinds(product, settings)
    colour = "" if "colour" in kinds.values() else find_colour(product.get("title", ""), settings, artist_name)
    return f"{artist_code.upper()}{number:03d}-{code}{colour}"


def variant_skus_from_base(product: dict, base: str, settings: dict) -> dict[str, str]:
    """Base PF-PINKTS -> PF-PINKTS-S, PF-PINKTS-M ... one per variant."""
    base = (base or "").strip().upper()
    if not base:
        return {}
    kinds = option_kinds(product, settings)
    result = {}
    for v in product.get("variants", []):
        sku = base
        other_parts, size_parts = [], []
        for name, value in (v.get("options") or {}).items():
            kind = kinds.get(name, "other")
            if kind == "colour":
                # colour joins the description: PF001-TS + GREY -> PF001-TSGREY
                sku += find_colour(value, settings) or _short_code(value)
            elif kind == "size":
                size_parts.append(size_code(value, settings) or _short_code(value))
            elif kind == "other":
                other_parts.append(_short_code(value))
        # size always goes last: BASE-OTHER-SIZE
        for part in other_parts + size_parts:
            if part:
                sku += "-" + part
        result[v["id"]] = sku
    return result


# ---------------------------------------------------------------------------
# Suggestions: the starting values shown in the editable tables
# ---------------------------------------------------------------------------


def _same_for_all(variants: list[dict], key: str) -> str:
    values = {str(v.get(key) or "").strip() for v in variants}
    values.discard("")
    return values.pop() if len(values) == 1 else ""


def country_label(code: str, settings: dict) -> str:
    """'HN' -> 'Honduras (HN)'"""
    code = (code or "").strip().upper()
    if not code:
        return ""
    return f"{settings.get('countries', {}).get(code, code)} ({code})"


def country_code(text) -> str:
    """'Honduras (HN)' or 'hn' -> 'HN'. Anything that isn't a 2-letter code gives ''."""
    t = _norm(text).upper()
    m = re.search(r"\(([A-Z]{2})\)\s*$", t)
    if m:
        return m.group(1)
    return t if re.fullmatch(r"[A-Z]{2}", t) else ""


def country_options(settings: dict, products: list[dict] | None = None) -> list[str]:
    """Dropdown choices: the usual clothing/merch countries first, then the rest, plus any already in use."""
    codes = list(settings.get("countries", {}))
    for p in products or []:
        for v in p.get("variants", []):
            c = (v.get("country") or "").upper()
            if c and c not in codes:
                codes.append(c)
    return [country_label(c, settings) for c in codes]


def suggest_product_row(product: dict, rule: Rule | None, settings: dict, base_sku: str = "",
                        prefill_country: bool = True) -> dict:
    """One row of the products table: current value if filled, otherwise a suggestion.
    The "suggested" list records which fields were filled in by the app."""
    variants = product.get("variants", [])
    mf = product.get("metafields", {})
    suggested = []

    def pick(current, suggestion, name):
        if not _blank(current):
            return current
        if not _blank(suggestion):
            suggested.append(name)
            return suggestion
        return ""

    # Country of origin is NEVER pre-filled. The box shows the current country only when every size
    # already has the same one; the usual countries are offered as suggestions in the dropdown.
    current_country = _same_for_all(variants, "country")
    country_missing = any(_blank(v.get("country")) for v in variants)
    country = country_label(current_country, settings) if current_country and not country_missing else ""
    partial_country = country_label(current_country, settings) if current_country and country_missing else ""

    row = {
        "product_id": product["id"],
        "title": product.get("title", ""),
        "kind": (rule.group if rule else "unknown") + (" · preorder" if is_preorder(product, settings) else ""),
        "product_type": pick(product.get("product_type"), rule.product_type if rule else "", "type"),
        "category": pick(product.get("category_name"), rule.category if rule else "", "category"),
        "tags": ", ".join(product.get("tags", [])),
        "blank": "",
        "size_guide": mf.get("size_guide") or "",
        "hs_code": pick(_same_for_all(variants, "hs_code"), rule.hs_code if rule else "", "hs_code"),
        "country": country,
        "partial_country": partial_country,
        "usual_codes": list(rule.usual_countries) if rule else [],
        "usual_country": " / ".join(settings.get("countries", {}).get(c, c) for c in (rule.usual_countries if rule else [])),
        "base_sku": base_sku,
    }
    row["suggested"] = ", ".join(suggested)
    return row


def suggest_variant_rows(
    product: dict,
    rule: Rule | None,
    settings: dict,
    sku_map: dict[str, str] | None = None,
    blank: Blank | None = None,
    overwrite_skus: bool = False,
) -> list[dict]:
    rows = []
    sku_map = sku_map or {}
    is_digital = bool(rule and rule.group == "digital")
    for v in product.get("variants", []):
        suggested = []
        sku = v.get("sku") or ""
        new_sku = sku_map.get(v["id"], "")
        if new_sku and (overwrite_skus or _blank(sku)) and new_sku != sku:
            sku = new_sku
            suggested.append("sku")
        weight = _to_float(v.get("weight_kg"))
        if not weight and not is_digital:
            standard = (blank.weight_kg if blank and blank.weight_kg else None) or (rule.weight_kg if rule else None)
            if standard:
                weight = standard
                suggested.append("weight")
        rows.append(
            {
                "product_id": product["id"],
                "variant_id": v["id"],
                "product": product.get("title", ""),
                "variant": v.get("title", ""),
                "sku": sku,
                "barcode": v.get("barcode") or "",
                "price": _to_float(v.get("price")),
                "cost": _to_float(v.get("cost")),
                "weight_kg": weight,
                "suggested": ", ".join(suggested),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Working out exactly what will change
# ---------------------------------------------------------------------------

@dataclass
class Change:
    product_id: str
    product_title: str
    level: str  # "product" / "variant" / "metafield"
    target_id: str  # product id or variant id
    field: str
    old: object
    new: object
    label: str = ""  # variant title, for display
    extra: dict = field(default_factory=dict)


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN from empty table cells
            return ""
        return f"{value:g}"
    return str(value).strip()


def _num_changed(old, new) -> bool:
    a, b = _to_float(old), _to_float(new)
    if b is None:
        return False  # never blank out a number by accident
    return a is None or abs(a - b) > 1e-9


def build_changes(
    product: dict,
    prod_row: dict,
    variant_rows: list[dict],
    settings: dict,
    size_guide_field: dict | None = None,
) -> tuple[list[Change], list[str]]:
    """Compares the edited tables with what's in Shopify now.
    Returns (changes, warnings). Blank cells never wipe existing data."""
    changes: list[Change] = []
    warnings: list[str] = []
    pid, title = product["id"], product.get("title", "")
    mf = product.get("metafields", {})

    def add(level, target, fld, old, new, label="", **extra):
        changes.append(Change(pid, title, level, target, fld, old, new, label, extra))

    # --- product fields ---
    new_type = _norm(prod_row.get("product_type"))
    if new_type and new_type != _norm(product.get("product_type")):
        add("product", pid, "product_type", product.get("product_type"), new_type)

    new_cat = _norm(prod_row.get("category"))
    if new_cat and new_cat != _norm(product.get("category_name")):
        add("product", pid, "category", product.get("category_name"), new_cat)

    new_tags = [t.strip() for t in _norm(prod_row.get("tags")).split(",") if t.strip()]
    if new_tags and sorted(new_tags) != sorted(t.strip() for t in product.get("tags", [])):
        add("product", pid, "tags", ", ".join(product.get("tags", [])), ", ".join(new_tags))

    # --- size guide (release date and tracklist are only checked, never written) ---
    new_guide = _norm(prod_row.get("size_guide"))
    if new_guide and new_guide != _norm(mf.get("size_guide")):
        if size_guide_field and size_guide_field.get("key"):
            add("metafield", pid, "size_guide", mf.get("size_guide"), new_guide,
                namespace=size_guide_field["namespace"], key=size_guide_field["key"], type=size_guide_field["type"])
        else:
            warnings.append(f"{title}: couldn't find a size guide field in this store, so the size guide wasn't saved.")

    # --- HS code and country of origin: both live on every variant ---
    raw_country = _norm(prod_row.get("country"))
    new_country = country_code(raw_country)
    if raw_country and not new_country:
        warnings.append(f"{title}: country of origin must be a 2-letter code like GB or CN (got '{raw_country}').")
    new_hs = re.sub(r"\D", "", _norm(prod_row.get("hs_code")))
    if new_hs and not 6 <= len(new_hs) <= 13:
        warnings.append(f"{title}: HS code must be 6–13 digits (got '{new_hs}').")
        new_hs = ""

    by_id = {v["id"]: v for v in product.get("variants", [])}
    for v in product.get("variants", []):
        if new_country and new_country != _norm(v.get("country")).upper():
            add("variant", v["id"], "country", v.get("country"), new_country, v.get("title", ""))
        if new_hs and new_hs != re.sub(r"\D", "", _norm(v.get("hs_code"))):
            add("variant", v["id"], "hs_code", v.get("hs_code"), new_hs, v.get("title", ""))

    # --- variant fields ---
    for row in variant_rows:
        v = by_id.get(row.get("variant_id"))
        if not v:
            continue
        label = v.get("title", "")
        for fld in ("sku", "barcode"):
            new = _norm(row.get(fld))
            if new and new != _norm(v.get(fld)):
                add("variant", v["id"], fld, v.get(fld), new, label)
        if _num_changed(v.get("price"), row.get("price")):
            add("variant", v["id"], "price", v.get("price"), _to_float(row.get("price")), label)
        if _num_changed(v.get("cost"), row.get("cost")):
            add("variant", v["id"], "cost", v.get("cost"), _to_float(row.get("cost")), label)
        if _num_changed(v.get("weight_kg"), row.get("weight_kg")):
            add("variant", v["id"], "weight_kg", v.get("weight_kg"), _to_float(row.get("weight_kg")), label)

    return changes, warnings
