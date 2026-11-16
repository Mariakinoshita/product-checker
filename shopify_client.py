"""
Talks to Shopify (Admin GraphQL API).

Reading: products, variants, metafields, existing SKUs, categories.
Writing: product type/category/tags, variant SKU/barcode/price/cost/weight/
country/HS code, and metafields (release date, tracklist, size guide).

Writing only ever happens inside apply_changes(), which the app only calls
after you've reviewed the changes and ticked the confirm box.
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

import requests

import rules
from rules import Change

WEIGHT_TO_KG = {"KILOGRAMS": 1.0, "GRAMS": 0.001, "POUNDS": 0.45359237, "OUNCES": 0.028349523125}


class ShopifyError(Exception):
    pass


def _uncategorised(node: dict) -> bool:
    cat = node.get("category") or {}
    return (not cat) or cat.get("id", "").endswith("/na") or (cat.get("fullName") or "").strip().lower() == "uncategorized"


def normalise_shop_domain(text: str) -> str:
    """Accepts any way of writing the store and returns 'xxx.myshopify.com':
    'queen-uk', 'queen-uk.myshopify.com', 'https://queen-uk.myshopify.com/admin',
    'admin.shopify.com/store/queen-uk', 'https://admin.shopify.com/store/queen-uk/products/123'."""
    t = (text or "").strip().lower()
    t = re.sub(r"^https?://", "", t)
    m = re.search(r"admin\.shopify\.com/store/([^/?#\s]+)", t)
    if m:
        t = m.group(1)
    t = t.split("/")[0].split("?")[0]
    if t.endswith(".myshopify.com"):
        t = t[: -len(".myshopify.com")]
    return f"{t}.myshopify.com" if t else ""


def _friendly(errors) -> str:
    text = json.dumps(errors)
    if "ACCESS_DENIED" in text or "access denied" in text.lower():
        return (
            "Shopify said 'access denied'. The app is probably missing a permission (scope). "
            "Check it has read_products, write_products and read_online_store_pages (if saving SKU/cost/weight "
            "fails, also add write_inventory), "
            "and that you clicked Release + updated the install on this store. Details: " + text
        )
    return text


def can_connect(shop: str, client_id: str, client_secret: str, session=None) -> str:
    """Tries to log in to one store. Returns "ok", "not installed" or "error: …".
    Only asks for a login pass; it doesn't read or change anything."""
    session = session or requests
    try:
        r = session.post(
            f"https://{normalise_shop_domain(shop)}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=15,
        )
    except Exception as e:  # no internet, store address doesn't exist…
        return f"error: {e.__class__.__name__}"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (400, 401, 403, 404):
        return "not installed"
    return f"error: HTTP {r.status_code}"


class ShopifyClient:
    def __init__(self, shop: str, client_id: str, client_secret: str, api_version: str, session=None):
        self.shop = normalise_shop_domain(shop)
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_version = api_version
        self.session = session or requests.Session()
        self._token = None
        self._token_expires = 0.0

    # ------------------------------------------------------------------ auth
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        r = self.session.post(
            f"https://{self.shop}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise ShopifyError(
                f"Couldn't log in to {self.shop} (HTTP {r.status_code}). Check the app is installed on this "
                f"store and the Client ID/Secret are right. {r.text[:300]}"
            )
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 86399))
        return self._token

    def granted_scopes(self) -> str:
        q = "{ currentAppInstallation { accessScopes { handle } } }"
        data = self.graphql(q)
        return ", ".join(s["handle"] for s in data["currentAppInstallation"]["accessScopes"])

    # --------------------------------------------------------------- graphql
    def graphql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            r = self.session.post(
                f"https://{self.shop}/admin/api/{self.api_version}/graphql.json",
                headers={"X-Shopify-Access-Token": self._get_token(), "Content-Type": "application/json"},
                data=json.dumps({"query": query, "variables": variables or {}}),
                timeout=60,
            )
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code != 200:
                raise ShopifyError(f"Shopify error HTTP {r.status_code}: {r.text[:500]}")
            body = r.json()
            errors = body.get("errors")
            if errors:
                if "THROTTLED" in json.dumps(errors):
                    time.sleep(2 + attempt * 2)
                    continue
                raise ShopifyError(_friendly(errors))
            return body["data"]
        raise ShopifyError("Shopify kept saying 'too many requests'. Wait a minute and try again.")

    # -------------------------------------------------------------- reading
    def _product_fields(self) -> str:
        return """
          id title handle productType tags status descriptionHtml
          category { id fullName }
          featuredMedia { id }
          metafields(first: 50) { nodes { namespace key type value } }
          variants(first: 60) { nodes {
            id title sku barcode price
            selectedOptions { name value }
            inventoryItem {
              id
              unitCost { amount }
              countryCodeOfOrigin
              harmonizedSystemCode
              measurement { weight { value unit } }
            }
          } }
        """

    @staticmethod
    def normalise_product(node: dict, settings: dict) -> dict:
        """Turns Shopify's answer into the simple shape rules.py works with."""
        variants = []
        for v in node["variants"]["nodes"]:
            inv = v.get("inventoryItem") or {}
            weight = ((inv.get("measurement") or {}).get("weight")) or {}
            weight_kg = None
            if weight.get("value") is not None:
                weight_kg = round(float(weight["value"]) * WEIGHT_TO_KG.get(weight.get("unit"), 1.0), 4)
            variants.append(
                {
                    "id": v["id"],
                    "inventory_item_id": inv.get("id"),
                    "title": v.get("title", ""),
                    "options": {o["name"]: o["value"] for o in v.get("selectedOptions", [])},
                    "sku": v.get("sku") or "",
                    "barcode": v.get("barcode") or "",
                    "price": v.get("price"),
                    "cost": (inv.get("unitCost") or {}).get("amount"),
                    "weight_kg": weight_kg,
                    "country": inv.get("countryCodeOfOrigin") or "",
                    "hs_code": inv.get("harmonizedSystemCode") or "",
                }
            )
        return {
            "id": node["id"],
            "title": node.get("title", ""),
            "handle": node.get("handle", ""),
            "product_type": node.get("productType") or "",
            # Shopify's "Uncategorized" counts as no category
            "category_id": "" if _uncategorised(node) else (node.get("category") or {}).get("id", ""),
            "category_name": "" if _uncategorised(node) else (node.get("category") or {}).get("fullName", ""),
            "raw_metafields": (node.get("metafields") or {}).get("nodes", []),
            "tags": node.get("tags", []),
            "has_image": bool(node.get("featuredMedia")),
            "description": node.get("descriptionHtml") or "",
            "status": node.get("status", ""),
            "metafields": rules.pick_metafields((node.get("metafields") or {}).get("nodes", []), settings),
            "size_guide_label": rules.size_guide_label((node.get("metafields") or {}).get("nodes", []), settings),
            "variants": variants,
        }

    def search_products(self, query: str, settings: dict, limit: int = 50) -> list[dict]:
        q = """
        query($q: String!, $after: String) {
          products(first: 2, query: $q, after: $after) {
            nodes { %s }
            pageInfo { hasNextPage endCursor }
          }
        }""" % self._product_fields()
        out, after = [], None
        while len(out) < limit:
            data = self.graphql(q, {"q": query, "after": after})["products"]
            batch = [self.normalise_product(n, settings) for n in data["nodes"]]
            self._add_linked_names(batch, settings)
            out += batch
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return out[:limit]

    def _add_linked_names(self, products: list[dict], settings: dict) -> None:
        """Size guide fields often hold links (gid://shopify/Page/123). Looks up the page / entry names
        in one request so 'Size guide now' can show 'EP01 Size Guide' instead of a code."""
        wanted = {}
        for p in products:
            for m in p.get("raw_metafields", []):
                if rules._matches("size_guide", m.get("namespace", ""), m.get("key", ""), settings):
                    for gid in re.findall(r"gid://shopify/[A-Za-z]+/\d+", str(m.get("value") or "")):
                        wanted[gid] = None
        if not wanted:
            return
        q = """query($ids: [ID!]!) { nodes(ids: $ids) {
                 id ... on Page { title } ... on Metaobject { displayName } } }"""
        ids = list(wanted)
        for i in range(0, len(ids), 100):
            for n in self.graphql(q, {"ids": ids[i:i + 100]})["nodes"]:
                if n:
                    wanted[n["id"]] = n.get("title") or n.get("displayName")
        for p in products:
            for m in p.get("raw_metafields", []):
                gids = re.findall(r"gid://shopify/[A-Za-z]+/\d+", str(m.get("value") or ""))
                names = [{"title": wanted[g]} for g in gids if wanted.get(g)]
                if names:
                    m["references"] = {"nodes": names}
            p["size_guide_label"] = rules.describe_size_guide(p["raw_metafields"], settings)

    def find_by_titles(self, titles: list[str], settings: dict) -> tuple[list[dict], list[str], dict]:
        """Returns (found products, titles not found, {title: close matches})."""
        found, not_found, close = [], [], {}
        seen = set()
        for title in titles:
            title = title.strip()
            if not title:
                continue
            escaped = title.replace("\\", "\\\\").replace('"', '\\"')
            results = self.search_products(f'title:"{escaped}"', settings, limit=6)
            exact = [p for p in results if p["title"].strip().lower() == title.lower()]
            if exact:
                for p in exact:
                    if p["id"] not in seen:
                        seen.add(p["id"])
                        found.append(p)
            else:
                not_found.append(title)
                if results:
                    close[title] = [p["title"] for p in results]
        return found, not_found, close

    def skus_starting_with(self, prefix: str, max_pages: int = 20) -> dict[str, str]:
        """{variant_id: sku} for every variant in the store whose SKU starts with prefix."""
        q = """
        query($q: String!, $after: String) {
          productVariants(first: 250, query: $q, after: $after) {
            nodes { id sku }
            pageInfo { hasNextPage endCursor }
          }
        }"""
        out, after = {}, None
        for _ in range(max_pages):
            data = self.graphql(q, {"q": f"sku:{prefix}*", "after": after})["productVariants"]
            for n in data["nodes"]:
                if n.get("sku"):
                    out[n["id"]] = n["sku"]
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return out

    def skus_in_use(self, skus: list[str]) -> dict[str, str]:
        """{variant_id: sku} for variants in the store that already use any of these SKUs."""
        out = {}
        q = "query($q: String!) { productVariants(first: 50, query: $q) { nodes { id sku } } }"
        for i in range(0, len(skus), 20):
            chunk = [s for s in skus[i : i + 20] if s]
            if not chunk:
                continue
            query = " OR ".join(f'sku:"{s}"' for s in chunk)
            for n in self.graphql(q, {"q": query})["productVariants"]["nodes"]:
                if n.get("sku"):
                    out[n["id"]] = n["sku"]
        return out

    def search_categories(self, term: str) -> list[dict]:
        q = "query($s: String!) { taxonomy { categories(first: 20, search: $s) { nodes { id name fullName isLeaf } } } }"
        return self.graphql(q, {"s": term})["taxonomy"]["categories"]["nodes"]

    def resolve_category(self, term: str) -> dict | None:
        """'T-Shirts' or 'Apparel & Accessories > ... > T-Shirts' -> {id, fullName}."""
        term = (term or "").strip()
        if not term:
            return None
        if term.startswith("gid://shopify/TaxonomyCategory/"):
            return {"id": term, "fullName": term}
        leaf = term.split(">")[-1].strip()
        nodes = self.search_categories(leaf)
        for n in nodes:
            if n["fullName"].lower() == term.lower():
                return n
        for n in nodes:
            if n["name"].lower() == leaf.lower():
                return n
        return nodes[0] if nodes else None

    def list_pages(self) -> list[dict]:
        """The store's pages (Online Store > Pages). Needs the read_online_store_pages permission."""
        q = """query($after: String) { pages(first: 250, after: $after) {
                 nodes { id title handle } pageInfo { hasNextPage endCursor } } }"""
        out, after = [], None
        for _ in range(10):
            data = self.graphql(q, {"after": after})["pages"]
            out += data["nodes"]
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return out

    def metafield_definitions(self) -> list[dict]:
        q = """{ metafieldDefinitions(first: 100, ownerType: PRODUCT) {
                 nodes { name namespace key type { name } } } }"""
        return [
            {"name": n["name"], "namespace": n["namespace"], "key": n["key"], "type": n["type"]["name"]}
            for n in self.graphql(q)["metafieldDefinitions"]["nodes"]
        ]

    def size_guide_field(self, settings: dict) -> dict | None:
        """The store field size guides are saved into (found by name, or set in settings.json)."""
        return rules.pick_definition(self.metafield_definitions(), "size_guide", settings)

    def size_guides_in_use(self, conf: dict, max_products: int = 500) -> list[dict]:
        """Every different size guide value already on products in this store, with a readable label."""
        if not conf.get("key"):
            return []
        q = """
        query($after: String) {
          products(first: 100, after: $after) {
            nodes {
              title
              sg: metafield(namespace: %s, key: %s) {
                value
                reference {
                  __typename
                  ... on Metaobject { displayName }
                  ... on Page { title }
                  ... on MediaImage { image { url } }
                  ... on GenericFile { url }
                }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }""" % (json.dumps(conf["namespace"]), json.dumps(conf["key"]))
        found: dict[str, dict] = {}
        after, seen = None, 0
        while seen < max_products:
            data = self.graphql(q, {"after": after})["products"]
            for n in data["nodes"]:
                seen += 1
                sg = n.get("sg")
                if not sg or not sg.get("value"):
                    continue
                ref = sg.get("reference") or {}
                label = (
                    ref.get("displayName")
                    or ref.get("title")
                    or (ref.get("image") or {}).get("url")
                    or ref.get("url")
                    or sg["value"][:80]
                )
                entry = found.setdefault(sg["value"], {"label": label, "value": sg["value"], "example_product": n["title"], "used_on": 0})
                entry["used_on"] += 1
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return sorted(found.values(), key=lambda e: -e["used_on"])

    # -------------------------------------------------------------- writing
    def apply_changes(self, changes: list[Change], category_ids: dict[str, str]) -> list[str]:
        """Writes the changes. Returns a list of result messages (errors start with ❌)."""
        messages = []
        by_product: dict[str, list[Change]] = {}
        for c in changes:
            by_product.setdefault(c.product_id, []).append(c)

        for pid, items in by_product.items():
            title = items[0].product_title

            # 1. Product fields
            product_input = {"id": pid}
            for c in items:
                if c.level != "product":
                    continue
                if c.field == "product_type":
                    product_input["productType"] = c.new
                elif c.field == "tags":
                    product_input["tags"] = [t.strip() for t in str(c.new).split(",") if t.strip()]
                elif c.field == "category":
                    cat_id = category_ids.get(c.new)
                    if cat_id:
                        product_input["category"] = cat_id
                    else:
                        messages.append(f"❌ {title}: couldn't find a Shopify category matching '{c.new}'.")
            if len(product_input) > 1:
                m = """mutation($p: ProductUpdateInput!) {
                         productUpdate(product: $p) { product { id } userErrors { field message } } }"""
                errs = self.graphql(m, {"p": product_input})["productUpdate"]["userErrors"]
                messages.append(f"❌ {title}: {errs}" if errs else f"✅ {title}: product details updated")

            # 2. Variant fields (one call per product)
            variant_inputs: dict[str, dict] = {}
            for c in items:
                if c.level != "variant":
                    continue
                vi = variant_inputs.setdefault(c.target_id, {"id": c.target_id})
                inv = vi.setdefault("inventoryItem", {})
                if c.field == "sku":
                    inv["sku"] = c.new
                elif c.field == "barcode":
                    vi["barcode"] = c.new
                elif c.field == "price":
                    vi["price"] = f"{float(c.new):.2f}"
                elif c.field == "cost":
                    inv["cost"] = f"{float(c.new):.2f}"
                elif c.field == "weight_kg":
                    inv["measurement"] = {"weight": {"value": float(c.new), "unit": "KILOGRAMS"}}
                elif c.field == "hs_code":
                    inv["harmonizedSystemCode"] = c.new
                elif c.field == "country":
                    inv["countryCodeOfOrigin"] = c.new
            for vi in variant_inputs.values():
                if not vi.get("inventoryItem"):
                    vi.pop("inventoryItem", None)
            if variant_inputs:
                m = """mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) {
                         productVariantsBulkUpdate(productId: $pid, variants: $v) {
                           productVariants { id } userErrors { field message } } }"""
                errs = self.graphql(m, {"pid": pid, "v": list(variant_inputs.values())})[
                    "productVariantsBulkUpdate"
                ]["userErrors"]
                messages.append(
                    f"❌ {title} (variants): {errs}" if errs else f"✅ {title}: {len(variant_inputs)} variant(s) updated"
                )

            # 3. Metafields
            mfs = [
                {
                    "ownerId": pid,
                    "namespace": c.extra["namespace"],
                    "key": c.extra["key"],
                    "type": c.extra["type"],
                    "value": str(c.new),
                }
                for c in items
                if c.level == "metafield"
            ]
            if mfs:
                m = """mutation($m: [MetafieldsSetInput!]!) {
                         metafieldsSet(metafields: $m) { metafields { id } userErrors { field message } } }"""
                errs = self.graphql(m, {"m": mfs})["metafieldsSet"]["userErrors"]
                messages.append(
                    f"❌ {title} (release date/tracklist/size guide): {errs}"
                    if errs
                    else f"✅ {title}: {len(mfs)} extra field(s) saved"
                )
        return messages


class DemoClient:
    """Pretend store so you can try the app without logging in to Shopify.
    Changes are kept in memory only — nothing is sent anywhere."""

    shop = "demo"

    def __init__(self, path: Path | str = Path(__file__).parent / "config" / "demo_products.json"):
        self.products = json.load(open(path, encoding="utf-8"))

    def granted_scopes(self) -> str:
        return "demo (nothing is written to Shopify)"

    def size_guide_field(self, settings):
        return {"name": "Size guide", "namespace": "custom", "key": "size_guide", "type": "multi_line_text_field"}

    def find_by_titles(self, titles, metafields):
        lookup = {p["title"].lower(): p for p in self.products}
        found, not_found, close = [], [], {}
        for t in titles:
            t = t.strip()
            if not t:
                continue
            if t.lower() in lookup:
                found.append(copy.deepcopy(lookup[t.lower()]))
            else:
                not_found.append(t)
                close[t] = [p["title"] for p in self.products if t.lower() in p["title"].lower()]
        return found, not_found, close

    def search_products(self, query, metafields, limit=250):
        q = query.lower().strip()
        return [copy.deepcopy(p) for p in self.products if not q or q in p["title"].lower()][:limit]

    def skus_starting_with(self, prefix):
        return {v["id"]: v["sku"] for p in self.products for v in p["variants"] if v["sku"].upper().startswith(prefix.upper())}

    def skus_in_use(self, skus):
        wanted = {s.upper() for s in skus}
        return {v["id"]: v["sku"] for p in self.products for v in p["variants"] if v["sku"].upper() in wanted}

    def resolve_category(self, term):
        return {"id": "demo-" + term, "fullName": f"(demo) {term}"} if term else None

    def list_pages(self):
        return [{"id": "gid://shopify/Page/1", "title": "Size Guide - AS Colour 5026 Staple Tee", "handle": "sg-5026"},
                {"id": "gid://shopify/Page/2", "title": "Size Guide - AS Colour 5148 Heavy Zip Hood", "handle": "sg-5148"},
                {"id": "gid://shopify/Page/3", "title": "Shipping & Returns", "handle": "shipping"}]

    def metafield_definitions(self):
        return [
            {"name": "Release date", "namespace": "custom", "key": "release_date", "type": "date"},
            {"name": "Tracklist", "namespace": "custom", "key": "tracklist", "type": "multi_line_text_field"},
            {"name": "Size guide", "namespace": "custom", "key": "size_guide", "type": "multi_line_text_field"},
        ]

    def size_guides_in_use(self, conf, max_products=500):
        return []

    def apply_changes(self, changes, category_ids):
        return [f"🧪 (demo) {c.product_title}: {c.field} {c.label} → {c.new}" for c in changes]
