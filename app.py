"""
Product Checker — checks Shopify products have all the info they need,
suggests what's missing, and (after you review it) writes it to Shopify.

Run locally:   streamlit run app.py
"""

from __future__ import annotations

import hmac
import io
import re

import pandas as pd
import streamlit as st

import rules
from shopify_client import DemoClient, ShopifyClient, ShopifyError, can_connect, normalise_shop_domain

st.set_page_config(page_title="Product Checker", page_icon="✅", layout="wide")


def password_gate():
    """If APP_PASSWORD is set in the secrets (it is on the online version), ask for it before
    showing anything. On your laptop without APP_PASSWORD, the app opens straight away."""
    try:
        expected = st.secrets.get("APP_PASSWORD")
    except Exception:
        expected = None
    if not expected or st.session_state.get("unlocked"):
        return
    st.title("✅ Product Checker")
    password = st.text_input("Team password", type="password")
    if password:
        if hmac.compare_digest(password.encode(), str(expected).encode()):
            st.session_state["unlocked"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


password_gate()

RULES = rules.load_rules()
BLANKS = rules.load_blanks()
SETTINGS = rules.load_settings()
STORES = rules.load_stores()

SKU_OFF = "Leave SKUs alone"
SKU_BASE = "Base SKU + sizes (I type the base, e.g. PF-PINKTS)"
SKU_AUTO = "Auto: ARTIST###-ITEMCOLOUR-SIZE (e.g. PF001-TSGREY-S)"

STATE_KEYS = ["pages", "products", "prod_df", "var_df", "changes", "category_ids", "results",
              "not_found", "close", "sku_mode_used", "store_guides", "dupes", "warnings", "size_guide_field"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reset_state():
    # also clears the tables' remembered edits, so they don't carry over to new products
    for k in STATE_KEYS + ["prod_df_final", "prod_editor", "var_editor"]:
        st.session_state.pop(k, None)
    for k in [k for k in st.session_state if str(k).startswith("pc_")]:  # the product card boxes
        st.session_state.pop(k, None)


def read_credentials() -> tuple[str, str] | None:
    """The Client ID/secret from .streamlit/secrets.toml (or Streamlit Cloud's Secrets box)."""
    try:
        return st.secrets["SHOPIFY_CLIENT_ID"], st.secrets["SHOPIFY_CLIENT_SECRET"]
    except Exception as e:
        st.sidebar.error(
            "Couldn't read SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET from .streamlit/secrets.toml, so only the demo "
            f"store is available. ({e.__class__.__name__}: {str(e)[:150]})")
        return None


@st.cache_data(ttl=3600, show_spinner="Checking which stores the app is installed on…")
def store_status(domains: tuple, client_id: str, client_secret: str) -> dict:
    """Tries to log in to every store in stores.csv at once (about 1–2 seconds in total).
    Remembered for an hour so it doesn't run on every click."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = pool.map(lambda d: can_connect(d, client_id, client_secret), domains)
    return dict(zip(domains, results))


def get_client(store: dict, creds):
    cache_key = "client_" + store["shop_domain"]
    if cache_key not in st.session_state:
        if store["shop_domain"] == "demo":
            st.session_state[cache_key] = DemoClient()
        else:
            st.session_state[cache_key] = ShopifyClient(store["shop_domain"], creds[0], creds[1], SETTINGS["api_version"])
    return st.session_state[cache_key]


def rule_for(product: dict):
    return rules.match_rule(product["title"], RULES, product.get("product_type", ""))


def is_page_field(field: dict | None) -> bool:
    return bool(field) and field.get("type") in rules.PAGE_FIELD_TYPES


def get_pages(client) -> list[dict]:
    if "pages" not in st.session_state:
        st.session_state["pages"] = client.list_pages()
    return st.session_state["pages"]


def guide_options(client, field: dict | None) -> dict:
    """{label shown in the dropdown: what it means}
    - Size guide field links to a page: one option per size guide page in the store (value = page id).
      Blanks from size_guides.csv are shown under their matching page.
    - Otherwise: the blanks' tables from size_guides.csv, plus guides already used in the store."""
    if is_page_field(field):
        try:
            pages = get_pages(client)
        except ShopifyError as e:
            st.error("Couldn't read the store's pages, so size guides can't be picked. " + str(e))
            return {}
        guide_pages = [p for p in pages if re.search(r"size|guide|chart|fit", p["title"], re.I)] or pages
        opts = {}
        for blank in BLANKS:
            page = rules.match_page_for_blank(blank.name, guide_pages)
            if page:
                opts[f"{blank.name} → {page['title']}"] = page["id"]
        for page in guide_pages:
            opts.setdefault(f"Page: {page['title']}", page["id"])
        return opts
    opts = {b.name: b for b in BLANKS}
    for g in st.session_state.get("store_guides") or []:
        opts[f"In store: {g['label']}"] = g["value"]
    return opts


def get_size_guide_field(client) -> dict | None:
    """Which product field size guides are saved into, found once per store."""
    if "size_guide_field" not in st.session_state:
        st.session_state["size_guide_field"] = client.size_guide_field(SETTINGS)
    return st.session_state["size_guide_field"]


def size_guide_value(choice, field: dict | None) -> str:
    if not choice:
        return ""
    if is_page_field(field):
        if isinstance(choice, rules.Blank):
            return ""  # a table can't go in a page-link field
        return rules.page_link_value(choice, field["type"])
    if isinstance(choice, rules.Blank):
        return choice.render(SETTINGS.get("size_guide_format", "auto"), (field or {}).get("type", ""))
    return choice or ""


def audit_table(products: list[dict]) -> pd.DataFrame:
    rows = []
    for p in products:
        rule = rule_for(p)
        res = rules.check_product(p, rule, SETTINGS)
        row = {"Product": p["title"], "Kind": (rule.group if rule else "unknown"),
               "Missing": sum(1 for v in res.values() if v != "ok")}
        for check, label in rules.CHECK_LABELS.items():
            row[label] = {"ok": "✅", "missing": "❌"}.get(res.get(check), "—")
        rows.append(row)
    return pd.DataFrame(rows)


def to_excel(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
    return buf.getvalue()


def build_tables(client, products: list[dict], sku_mode: str, artist_code: str, artist_name: str, overwrite: bool,
                 start_number: str = "") -> pd.DataFrame:
    """The starting values for each product card: what's in Shopify now, plus suggestions."""
    numbers_used: set[int] = set()
    if sku_mode == SKU_AUTO and artist_code:
        store_skus = client.skus_starting_with(artist_code)
        numbers_used = rules.sku_numbers_in_use(store_skus.values(), artist_code)
    next_number = max(numbers_used, default=0) + 1
    if start_number.strip().isdigit():
        next_number = int(start_number)

    prod_rows = []
    for p in products:
        rule = rule_for(p)
        number, desc = "", ""
        needs_sku = overwrite or any(not (v.get("sku") or "").strip() for v in p["variants"])
        if sku_mode == SKU_AUTO and artist_code and needs_sku:
            existing = rules.product_sku_number(p, artist_code)
            if existing is None:
                existing = next_number
                next_number += 1
            number = f"{existing:03d}"
            desc = rules.sku_description(p, rule, SETTINGS, artist_name)
        row = rules.suggest_product_row(p, rule, SETTINGS, rules.compose_base_sku(artist_code, number, desc))
        checks = rules.check_product(p, rule, SETTINGS)
        row.update({
            "include": True,
            "blank": "",
            "current_size_guide": p.get("size_guide_label") or "No size guide",
            "needs_size_guide": bool(rule and rule.needs_size_guide),
            "sku_number": number,
            "sku_desc": desc,
            "missing": ", ".join(rules.CHECK_LABELS[c] for c, v in checks.items() if v != "ok"),
            "sizes": len(p["variants"]),
        })
        row.pop("size_guide", None)
        prod_rows.append(row)
    return pd.DataFrame(prod_rows)


def build_variant_table(products: list[dict], prod_df: pd.DataFrame, overwrite: bool) -> pd.DataFrame:
    by_id = {p["id"]: p for p in products}
    blanks = {b.name: b for b in BLANKS}
    rows = []
    for _, prow in prod_df.iterrows():
        if not prow.get("include", True):
            continue
        p = by_id[prow["product_id"]]
        rule = rule_for(p)
        sku_map = rules.variant_skus_from_base(p, prow.get("base_sku") or "", SETTINGS)
        blank = blanks.get(prow.get("blank") or "")
        rows += rules.suggest_variant_rows(p, rule, SETTINGS, sku_map, blank, overwrite)
    cols = ["product_id", "variant_id", "product", "variant", "sku", "barcode", "price", "cost", "weight_kg", "suggested"]
    return pd.DataFrame(rows, columns=cols)


def clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return None
    return value


PREORDER_TAG = SETTINGS.get("preorder_tag_to_add", "Preorder")
NO_CHANGE = "— leave as it is —"


def _label(text: str, field: str, suggested: str) -> str:
    """Adds ✨ to a box's label when the app filled it in."""
    return f"{text} ✨" if field in [s.strip() for s in suggested.split(",")] else text


def render_product_card(row: dict, options: dict, sku_mode: str, artist_code: str, add_preorder: bool) -> dict:
    """One bordered card per product, with proper boxes and dropdowns. Returns the row with your edits."""
    pid = row["product_id"]
    key = lambda name: f"pc_{name}_{pid}"
    sug = row.get("suggested") or ""
    out = dict(row)
    with st.container(border=True):
        head, toggle = st.columns([5, 1])
        head.markdown(f"#### {row['title']}")
        info = f"{row['kind']} · {row['sizes']} size{'s' if row['sizes'] != 1 else ''}"
        head.caption(info + (f" · :red[Missing: {row['missing']}]" if row["missing"] else " · ✅ nothing missing"))
        out["include"] = toggle.toggle("Save", value=bool(row.get("include", True)), key=key("include"),
                                       help="Switch off to leave this product completely untouched")
        if not out["include"]:
            st.caption("This product won't be changed.")
            return out

        c1, c2, c3 = st.columns(3)
        out["product_type"] = c1.text_input(_label("Type", "type", sug), value=row["product_type"], key=key("type"))
        out["category"] = c2.text_input(
            _label("Category", "category", sug), value=row["category"], key=key("category"),
            help="A name like 'T-Shirts' is fine: the exact Shopify category is looked up when you review")
        tags = c3.text_input("Tags", value=row["tags"], key=key("tags"), help="Separate tags with commas")
        if add_preorder and not any(t.strip().lower() in [p.lower() for p in SETTINGS.get("preorder_tags", [])]
                                    for t in tags.split(",")):
            tags = ", ".join([t for t in [tags.strip(), PREORDER_TAG] if t])
            c3.caption(f"+ {PREORDER_TAG} will be added")
        out["tags"] = tags

        c4, c5, c6 = st.columns(3)
        out["hs_code"] = c4.text_input(_label("HS code", "hs_code", sug), value=row["hs_code"], key=key("hs_code"),
                                       help="6–13 digits, no dots")

        usual = [rules.country_label(c, SETTINGS) for c in row.get("usual_codes") or []]
        others = [o for o in rules.country_options(SETTINGS, st.session_state.get("products")) if o not in usual]
        choices = [""] + usual + others
        if row["country"] and row["country"] not in choices:
            choices.append(row["country"])
        out["country"] = c5.selectbox(
            "Country of origin", choices, index=choices.index(row["country"]) if row["country"] else 0,
            key=key("country"),
            format_func=lambda o: (("⭐ " if o in usual else "") + o) if o else
            (NO_CHANGE if row["country"] else "— choose —"),
            help="⭐ = usual for this kind of product. Applies to every size.")
        if row.get("partial_country"):
            c5.caption(f"Some sizes already have {row['partial_country']}")
        elif not row["country"] and usual:
            c5.caption("Usually: " + ", ".join(u.split(" (")[0] for u in usual))

        if row.get("needs_size_guide"):
            guide_labels = [""] + list(options.keys())
            out["blank"] = c6.selectbox("Size guide", guide_labels, key=key("blank"),
                                        format_func=lambda o: o or NO_CHANGE,
                                        help="Pick the blank or size guide page to link to this product")
            c6.caption(f"Now: {row['current_size_guide']}")
        else:
            out["blank"] = ""

        if sku_mode == SKU_AUTO:
            s1, s2, s3 = st.columns(3)
            number = s1.text_input(_label("SKU number", "sku", "sku" if row.get("sku_number") else ""),
                                   value=row.get("sku_number", ""), key=key("sku_number"),
                                   help=f"The number after the artist code, e.g. 001 in {artist_code or 'QUE'}001")
            desc = s2.text_input("SKU item + colour", value=row.get("sku_desc", ""), key=key("sku_desc"),
                                 help="e.g. TSGREY. Sizes are added on the end automatically")
            out["base_sku"] = rules.compose_base_sku(artist_code, number, desc)
            if number and not number.strip().isdigit():
                s3.error("SKU number must be digits only")
            elif out["base_sku"]:
                s3.caption("SKUs will look like")
                s3.code(out["base_sku"] + ("-S" if row["sizes"] > 1 else ""), language=None)
            elif not row.get("sku_number"):
                s3.caption("Already has SKUs, so none are suggested")
        elif sku_mode == SKU_BASE:
            out["base_sku"] = st.text_input("Base SKU", value=row.get("base_sku", ""), key=key("base_sku"),
                                            help="Sizes are added on the end: PF-PINKTS → PF-PINKTS-S, -M, -L")
    return out


PRETTY = {"product_type": "Type", "category": "Category", "tags": "Tags", "sku": "SKU", "barcode": "Barcode",
          "price": "Price", "cost": "Cost per item", "weight_kg": "Weight (kg)", "hs_code": "HS code",
          "country": "Country of origin", "size_guide": "Size guide"}


def _show(value) -> str:
    return "" if value in (None, "") else str(value)[:80]


def _was(olds: list) -> str:
    olds = {_show(o) for o in olds}
    if olds == {""}:
        return "was empty"
    if len(olds) == 1:
        return f"was {olds.pop()}"
    return "had different values"


def changes_table(changes) -> pd.DataFrame:
    """Every change as one flat list (for the Excel download and the 'one list' view)."""
    return pd.DataFrame([
        {"Product": c.product_title, "Size": c.label if c.level == "variant" else "",
         "Field": PRETTY.get(c.field, c.field), "Now": _show(c.old),
         "Will become": _show(c.new) + (f"  {c.label}" if c.field == "category" else "")}
        for c in changes])


def render_review(changes) -> None:
    """The review, grouped by product. Changes that are the same on every size are shown once."""
    by_product: dict[str, list] = {}
    for c in changes:
        by_product.setdefault(c.product_id, []).append(c)
    st.markdown(f"**{len(by_product)} product{'s' if len(by_product) != 1 else ''} · {len(changes)} changes**")
    guide_names = {v: k for k, v in guide_options_cache().items() if isinstance(v, str)}

    for items in by_product.values():
        title = items[0].product_title
        with st.container(border=True):
            st.markdown(f"#### {title}")
            lines = []
            for c in items:
                if c.level == "variant":
                    continue
                new = _show(c.new)
                if c.field == "category":
                    new = c.label.replace("→ ", "") or new
                if c.field == "size_guide":
                    new = guide_names.get(c.new) or ("new size guide table" if str(c.new).lstrip()[:1] in "<{[" else new)
                old = _show(c.old) or "empty"
                lines.append(f"- **{PRETTY.get(c.field, c.field)}:** {new}  \n  :gray[was {old}]")

            per_size: dict[str, dict] = {}
            by_field: dict[str, list] = {}
            for c in items:
                if c.level == "variant":
                    by_field.setdefault(c.field, []).append(c)
            for field_name, fcs in by_field.items():
                if len({_show(c.new) for c in fcs}) == 1:  # same new value on every size -> one line
                    n = len(fcs)
                    lines.append(f"- **{PRETTY.get(field_name, field_name)}:** {_show(fcs[0].new)}"
                                 f" on {n} size{'s' if n != 1 else ''}  \n  :gray[{_was([c.old for c in fcs])}]")
                else:
                    for c in fcs:
                        cell = _show(c.new) if _show(c.old) == "" else f"{_show(c.new)} (was {_show(c.old)})"
                        per_size.setdefault(c.label or "—", {})[PRETTY.get(field_name, field_name)] = cell
            if lines:
                st.markdown("\n".join(lines))
            if per_size:
                table = pd.DataFrame.from_dict(per_size, orient="index").fillna("")
                table.index.name = "Size"
                st.dataframe(table.reset_index(), hide_index=True, width="stretch")


def guide_options_cache() -> dict:
    """The size guide choices last shown on the cards (to show names instead of page codes)."""
    return st.session_state.get("guide_options_shown", {})


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("✅ Product Checker")
creds = read_credentials()
real_stores = [s for s in STORES if s["shop_domain"] != "demo"]
demo_stores = [s for s in STORES if s["shop_domain"] == "demo"]
status = store_status(tuple(s["shop_domain"] for s in real_stores), *creds) if creds and real_stores else {}
connected = [s for s in real_stores if status.get(s["shop_domain"]) == "ok"]
not_connected = [s for s in real_stores if status.get(s["shop_domain"]) != "ok"]

choices = connected + demo_stores
store_name = st.sidebar.selectbox("Store", [s["store_name"] for s in choices], on_change=reset_state,
                                  help="Only stores the product-checker app is installed on are listed")
store = next((s for s in choices if s["store_name"] == store_name), None)
if store is None:
    st.title("✅ Product Checker")
    if not creds:
        st.error("No stores available: the app can't find SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET. "
                 "Online: Manage app → ⋮ → Settings → Secrets. On your laptop: .streamlit/secrets.toml.")
    elif not real_stores:
        st.error("No stores listed in config/stores.csv yet. Add a line per store, e.g. "
                 "`Queen UK,queen-uk.myshopify.com,QUE,Queen`.")
    else:
        st.error("None of the stores in config/stores.csv would log in. Open 'store(s) not available' in the "
                 "sidebar to see why, install product-checker on the store, then click Refresh store list.")
    st.stop()
st.sidebar.caption(f"{len(connected)} store(s) connected")
if not_connected:
    with st.sidebar.expander(f"{len(not_connected)} store(s) not available"):
        for s in not_connected:
            st.write(f"• {s['store_name']}: {status.get(s['shop_domain'], 'not checked')}")
        st.caption("'not installed' = install product-checker on that store in the Dev Dashboard, then click Refresh.")
if st.sidebar.button("Refresh store list"):
    store_status.clear()
    st.rerun()
client = get_client(store, creds)

st.sidebar.subheader("SKUs")
sku_mode = st.sidebar.radio("How should SKUs be made?", [SKU_OFF, SKU_BASE, SKU_AUTO], index=0)
artist_code = st.sidebar.text_input("Artist code", value=store.get("artist_code", ""),
                                    help="Start of every SKU for this store, e.g. PF for Pink Floyd").strip().upper()
artist_name = st.sidebar.text_input("Artist name", value=store.get("artist_name", ""),
                                    help="So words in the artist's name (e.g. 'Pink' in Pink Floyd) aren't read as colours")
start_number = ""
if sku_mode == SKU_AUTO:
    start_number = st.sidebar.text_input(
        "Start SKU numbers at (optional)", placeholder="automatic",
        help="Leave empty to carry on after the highest number already used in the store, e.g. type 20 to "
             "start at QUE020. You can also change each product's number on its card.").strip()
overwrite_skus = st.sidebar.checkbox("Replace SKUs that already exist", value=False,
                                     help="Off = only fill in blank SKUs")
if store["shop_domain"] == "demo":
    st.sidebar.info("Demo store: fake products, nothing is sent to Shopify.")
if sku_mode == SKU_AUTO and not artist_code:
    st.sidebar.warning("Type an artist code to use Auto SKUs (e.g. PF). Without one, no SKUs are suggested.")

tab_check, tab_settings = st.tabs(["Check & fix products", "Store setup helpers"])

# ---------------------------------------------------------------------------
# Tab 1: check & fix
# ---------------------------------------------------------------------------
with tab_check:
    st.header("1. Choose products")
    how = st.radio("Find products by", ["Pasting titles", "Searching the store"], horizontal=True)
    if how == "Pasting titles":
        titles_text = st.text_area("Product titles (one per line, exactly as in Shopify)", height=150)
        search_text = ""
    else:
        search_text = st.text_input(
            "Search", placeholder="e.g. Animals 50   ·   tag:preorder   ·   product_type:Vinyl   ·   status:draft",
            help="Uses Shopify's own search, so anything that works in the admin search bar works here.")
        titles_text = ""

    if st.button("Load products", type="primary"):
        reset_state()
        try:
            with st.spinner("Reading products from Shopify…"):
                if how == "Pasting titles":
                    titles = [t for t in titles_text.splitlines() if t.strip()]
                    products, not_found, close = client.find_by_titles(titles, SETTINGS)
                else:
                    products, not_found, close = client.search_products(search_text, SETTINGS), [], {}
                st.session_state["products"] = products
                st.session_state["not_found"] = not_found
                st.session_state["close"] = close
                st.session_state["prod_df"] = build_tables(client, products, sku_mode, artist_code, artist_name,
                                                           overwrite_skus, start_number)
                st.session_state["sku_mode_used"] = (sku_mode, artist_code, overwrite_skus, start_number)
        except ShopifyError as e:
            st.error(str(e))

    products = st.session_state.get("products")
    for t in st.session_state.get("not_found", []):
        close = st.session_state.get("close", {}).get(t)
        st.warning(f"Couldn't find **{t}**" + (f" — did you mean: {', '.join(close[:5])}?" if close else ""))
    if products == []:
        st.info("No products found.")
    if products:
        if st.session_state.get("sku_mode_used") != (sku_mode, artist_code, overwrite_skus, start_number):
            st.warning("You changed the sidebar settings — click **Load products** again to use them.")

        # ---------------- audit ----------------
        st.header("2. What's missing")
        audit = audit_table(products)
        c1, c2, c3 = st.columns(3)
        c1.metric("Products checked", len(audit))
        c2.metric("All complete", int((audit["Missing"] == 0).sum()))
        c3.metric("Need fixing", int((audit["Missing"] > 0).sum()))
        st.caption("✅ = there · ❌ = missing · — = not needed for this kind of product")
        st.dataframe(audit, hide_index=True, width="stretch")
        st.download_button("Download this check as Excel", to_excel({"Check": audit}),
                           file_name=f"product_check_{store['shop_domain']}.xlsx")

        # ---------------- product-level fixes ----------------
        st.header("3. Fix product details")
        st.caption("Each card starts with what's in Shopify now. ✨ marks a box the app filled in for you: it **will** "
                   "be saved unless you change or clear it. Empty boxes are never saved, so nothing gets wiped.")
        try:
            guide_field = get_size_guide_field(client)
        except ShopifyError as e:
            st.error(str(e))
            guide_field = None
        options = guide_options(client, guide_field)
        st.session_state["guide_options_shown"] = {k: v for k, v in options.items() if isinstance(v, str)}
        if guide_field:
            st.caption(f"Size guides are saved to **{guide_field['namespace']}.{guide_field['key']}**"
                       + (" (links to a page)" if is_page_field(guide_field) else ""))

        add_preorder = st.checkbox(
            f"Add the **{PREORDER_TAG}** tag to all products below",
            key="pc_all_preorder",
            help="Adds the tag to every ticked product that doesn't already have a preorder tag. "
                 "Existing tags are kept.")

        card_rows = [render_product_card(row, options, sku_mode, artist_code, add_preorder)
                     for row in st.session_state["prod_df"].to_dict("records")]
        prod_df = pd.DataFrame(card_rows)

        by_id = {p["id"]: p for p in products}

        if st.button("Next: variant details →"):
            st.session_state["prod_df_final"] = prod_df.copy()
            st.session_state.pop("var_editor", None)
            st.session_state["var_df"] = build_variant_table(products, prod_df, overwrite_skus)
            for k in ("changes", "results", "dupes", "warnings"):
                st.session_state.pop(k, None)

        # ---------------- variant-level fixes ----------------
        if "var_df" in st.session_state:
            st.header("4. Fix variant details")
            st.caption("If you go back and change step 3, click **Next** again to rebuild this table "
                       "(edits made here will be reset).")
            var_df = st.data_editor(
                st.session_state["var_df"],
                key="var_editor",
                hide_index=True,
                width="stretch",
                disabled=["product", "variant", "suggested"],
                column_order=["product", "variant", "sku", "barcode", "price", "cost", "weight_kg", "suggested"],
                column_config={
                    "price": st.column_config.NumberColumn("Price", format="%.2f", min_value=0),
                    "cost": st.column_config.NumberColumn("Cost per item", format="%.2f", min_value=0),
                    "weight_kg": st.column_config.NumberColumn("Weight (kg)", format="%.3f", min_value=0),
                    "barcode": st.column_config.TextColumn("Barcode"),
                    "sku": st.column_config.TextColumn("SKU"),
                    "suggested": st.column_config.TextColumn("suggested", width="small"),
                },
            )

            if st.button("Review changes"):
                prod_final = st.session_state["prod_df_final"]
                all_changes, warnings = [], []
                var_records = var_df.to_dict("records")
                for _, prow in prod_final.iterrows():
                    if not prow.get("include", True):
                        continue  # unticked: nothing is changed on this product
                    p = by_id[prow["product_id"]]
                    row = {k: clean(v) for k, v in prow.to_dict().items()}
                    row["size_guide"] = size_guide_value(options.get(row.get("blank") or ""), guide_field)
                    vrows = [{k: clean(v) for k, v in r.items()} for r in var_records if r["product_id"] == p["id"]]
                    changes, warns = rules.build_changes(p, row, vrows, SETTINGS, guide_field)
                    all_changes += changes
                    warnings += warns

                # Look up the exact Shopify category for anything typed in
                category_ids = {}
                try:
                    with st.spinner("Checking categories and SKUs with Shopify…"):
                        for term in {c.new for c in all_changes if c.field == "category"}:
                            found = client.resolve_category(term)
                            if found:
                                category_ids[term] = found["id"]
                                for c in all_changes:
                                    if c.field == "category" and c.new == term:
                                        c.label = f"→ {found['fullName']}"
                            else:
                                warnings.append(f"No Shopify category matches '{term}' — type a different name.")

                        # Duplicate SKU check: within these products and against the rest of the store
                        final_skus = {r["variant_id"]: (clean(r["sku"]) or "") for r in var_records}
                        new_skus = sorted({c.new for c in all_changes if c.field == "sku"})
                        loaded_ids = set(final_skus)
                        store_skus = {vid: s for vid, s in client.skus_in_use(new_skus).items() if vid not in loaded_ids}
                        dupe_ids = rules.find_duplicate_skus(final_skus, store_skus)
                    # categories that couldn't be matched aren't saved
                    all_changes = [c for c in all_changes if c.field != "category" or c.new in category_ids]
                    st.session_state["changes"] = all_changes
                    st.session_state["category_ids"] = category_ids
                    st.session_state["warnings"] = warnings
                    st.session_state["dupes"] = sorted({final_skus[v] for v in dupe_ids})
                    st.session_state.pop("results", None)
                except ShopifyError as e:
                    st.error(str(e))

        # ---------------- review & write ----------------
        if "changes" in st.session_state:
            st.header("5. Review and save to Shopify")
            changes = st.session_state["changes"]
            for w in st.session_state.get("warnings", []):
                st.warning(w)
            dupes = st.session_state.get("dupes", [])
            if dupes:
                st.error("These SKUs are used more than once (here or elsewhere in the store) — fix them in step 4 "
                         "and click Review again: " + ", ".join(dupes))
            if not changes:
                st.success("Nothing to change.")
            else:
                diff = changes_table(changes)
                render_review(changes)
                with st.expander("Show every change as one list"):
                    st.dataframe(diff, hide_index=True, width="stretch")
                st.download_button("Download these changes as Excel", to_excel({"Changes": diff}),
                                   file_name=f"product_changes_{store['shop_domain']}.xlsx")
                confirm = st.checkbox(f"I've checked these {len(changes)} changes and want to save them to "
                                      f"**{store['store_name']}**")
                if st.button("Save to Shopify", type="primary", disabled=not confirm or bool(dupes)):
                    try:
                        with st.spinner("Saving…"):
                            st.session_state["results"] = client.apply_changes(changes, st.session_state["category_ids"])
                    except ShopifyError as e:
                        st.error(str(e))

            for msg in st.session_state.get("results", []):
                (st.error if msg.startswith("❌") else st.write)(msg)
            if st.session_state.get("results"):
                st.info("Click **Load products** again to re-check them.")

# ---------------------------------------------------------------------------
# Tab 2: setup helpers
# ---------------------------------------------------------------------------
with tab_settings:
    st.header("Connection")
    if st.button("Test connection"):
        try:
            st.success(f"Connected to {client.shop}. Permissions: {client.granted_scopes()}")
        except ShopifyError as e:
            st.error(str(e))

    st.header("Where release date, tracklist and size guide live")
    st.caption("The app finds these by name: any product field with 'release', 'track' or 'size' in its key. "
               "Release dates also count if the description mentions one (e.g. 'Ships late March'), and "
               "tracklists count if the description has one. Click below to see what it picked for this store.")
    if st.button("Show product fields"):
        try:
            defs = client.metafield_definitions()
            st.dataframe(pd.DataFrame(defs), hide_index=True, width="stretch")
            for alias, label in [("release_date", "Release date"), ("tracklist", "Tracklist"), ("size_guide", "Size guide")]:
                found = rules.pick_definition(defs, alias, SETTINGS)
                if found:
                    st.write(f"✅ **{label}** → `{found['namespace']}.{found['key']}` ({found['type']})")
                else:
                    st.write(f"⚠️ **{label}**: no field found by name, so only the description is checked")
        except ShopifyError as e:
            st.error(str(e))

    st.header("Size guides")
    blank_names = [b.name for b in BLANKS]
    if blank_names:
        preview = st.selectbox("Preview a blank's size guide", blank_names)
        st.markdown(next(b for b in BLANKS if b.name == preview).as_html(), unsafe_allow_html=True)
    st.caption("Find size guides already on products in this store, so you can reuse them from the Blank dropdown.")
    if st.button("Find size guides already in this store"):
        try:
            with st.spinner("Looking through products…"):
                field = get_size_guide_field(client)
                st.session_state["store_guides"] = client.size_guides_in_use(field or {})
        except ShopifyError as e:
            st.error(str(e))
    if st.session_state.get("store_guides") is not None:
        guides = st.session_state["store_guides"]
        if guides:
            st.dataframe(pd.DataFrame(guides), hide_index=True, width="stretch")
        else:
            st.info("No size guides found on products in this store.")

    st.header("Rules in use")
    st.caption("Edit config/product_rules.csv (types, categories, HS codes, standard weights) and "
               "config/size_guides.csv (blanks) to change these.")
    st.dataframe(pd.read_csv(rules.CONFIG_DIR / "product_rules.csv", dtype=str).fillna(""),
                 hide_index=True, width="stretch")
    st.dataframe(pd.read_csv(rules.CONFIG_DIR / "size_guides.csv", dtype=str).fillna(""),
                 hide_index=True, width="stretch")
