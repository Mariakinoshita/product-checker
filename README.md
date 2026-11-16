# Product Checker

Checks that Shopify products have all the info they need, fills in what it can,
and saves the fixes to Shopify **only after you've reviewed them and ticked a
confirm box**.

## What it checks

| Every product | Music (vinyl, CD, cassette, box set) | Digital downloads | Clothing | Other merch |
|---|---|---|---|---|
| Title, image, type, category, tags, price, cost per item, SKU | + barcode, weight, country of origin, HS code, release date ⚑, tracklist ⚑ | + release date ⚑, tracklist ⚑ | + weight, country, HS code, size guide | + weight, country, HS code |

Anything tagged **preorder** also needs a release date ⚑.

⚑ = **flagged only**. The app tells you when it's missing but never writes it.
Release date and tracklist are found by the app itself:
- **Release date**: any product field with "release" in its name, or a description
  that mentions one ("Release date…", "Ships late March", "Expected dispatch…").
  You can change those description words in `settings.json`.
- **Tracklist**: any product field with "track" in its name, or a description with a
  tracklist in it ("Tracklist", "Side A", "Disc 1"…).

## What it fills in for you

- **Type, category, HS code and standard weight**, worked out from the title, using
  `config/product_rules.csv`. Your HS codes and weights are already in there.
- **Size guides**: the app finds the store's size guide field by itself (any field
  with "size" in its name).
  - If that field **links to a page** (Online Store → Pages), the dropdown lists your
    size guide pages, and blanks are matched to pages by style number (e.g. "AS Colour
    5026 → Size Guide 5026"). The app only links the product to the page. It never
    edits or creates pages.
  - If it's a **text** field, the blank's table from `size_guides.csv` is saved in it.
- **Country of origin**: pick it from a dropdown. The **Usually** column shows the usual
  countries for that kind of product (set in the `usual_countries` column of `product_rules.csv`):
  - Clothing: Honduras / Bangladesh / India / China / Pakistan. **Never pre-filled**, you pick.
  - Posters and stickers: United Kingdom. Gadgets and trinkets (mugs, pins, keyrings, magnets…): China.
    These are pre-filled because there's only one usual answer. Turn this off in the sidebar.
  - If some sizes have a country and others don't, the same country is suggested for the missing sizes.
- **SKUs**, two ways (choose in the sidebar):
  - *Base SKU + sizes*: type `PF-PINKTS` and you get `PF-PINKTS-S`, `PF-PINKTS-M`…
  - *Auto*: `PF001-TSGREY-S`. The app carries on from the highest PF number
    already in the store.
- **Safety**:
  - It never overwrites anything already filled in unless you change it yourself.
    The one exception is the "Replace SKUs that already exist" box.
  - Empty cells are never saved.
  - Any SKU used twice blocks saving.

---

## Setup (one time)

### 1. Make a new Shopify app for this

This app **changes** products, so keep it separate from your read-only report apps.

1. Shopify **Dev Dashboard → Create app**, name it e.g. `product-checker`.
2. **Versions → Access scopes**: `read_products,write_products,read_online_store_pages`
   (`read_online_store_pages` is read-only. It lets the app list your size guide pages so it can link products to them.)
   (SKU, cost, weight and HS code are saved through the product permission. If saving those ever
   fails with "access denied", add `write_inventory`. The app never touches stock levels.)
3. **Release**, then **install** it on the store you're testing with.
4. Copy the **Client ID** and **Client secret**.

### 2. Run it

**On your laptop (easiest for a first test)**, in PowerShell inside this folder:
```
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
notepad .streamlit\secrets.toml        # paste the Client ID and secret, save
pip install -r requirements.txt
streamlit run app.py
```

**Online, for the team (Streamlit Community Cloud, public link + team password)**
1. Make a new **public** GitHub repo (e.g. `product-checker`). Public is safe: no passwords are in the code.
2. Upload everything in the folder **except** `venv`, `.streamlit` and `__pycache__`.
3. On share.streamlit.io: **Create app** → repo, branch `main`, file `app.py`.
4. **Advanced settings → Secrets**, paste:
   ```
   SHOPIFY_CLIENT_ID = "..."
   SHOPIFY_CLIENT_SECRET = "..."
   APP_PASSWORD = "your-team-password"
   ```
5. **Deploy**. Anyone opening the link has to type the team password before they see anything.

To change the password later: Streamlit → your app → **⋮ → Settings → Secrets**.
On your laptop, the app only asks for a password if you also add `APP_PASSWORD` to your local `secrets.toml`.

### 3. Add your stores to the dropdown

The **Store** dropdown is built from `config/stores.csv`. When the app starts, it
tries to log in to each store listed there, and **only lists the ones that work**
(i.e. `product-checker` is installed on them). The rest appear under
"store(s) not available" in the sidebar, with the reason.

To add a store:
1. Install `product-checker` on it in the Dev Dashboard.
2. Add a line to `config/stores.csv`: name, `.myshopify.com` address, artist code, artist name. For example:
   `McFly,mc-fly-merch.myshopify.com,MCF,McFly`
3. Save, then click **Refresh store list** in the sidebar (or restart the app).

The artist code is the start of the store's SKUs (e.g. PF). It's only needed for Auto SKUs,
and you can also type it in the sidebar.

---

## First test: suggested order

1. **Store setup helpers → Test connection.** You should see the four permissions.
2. **Store setup helpers → Show product fields.** This shows which fields it
   picked for release date, tracklist and size guide. If it picked the wrong
   one, put the right namespace/key in `config/settings.json`.
3. **Store setup helpers → Find size guides already in this store.** Look at
   how existing size guides are written (an HTML table? plain text?). The app
   saves your blanks as an **HTML table** in text fields, **one line per row** in
   rich text fields, or **JSON** in JSON fields. If the existing ones look
   different, tell Claude and the format can be matched.
4. Back on **Check & fix**, paste **one or two titles of a draft product** and click Load.
5. Go through the steps: Fix product details → Next → Fix variant details →
   Review changes → tick the box → Save.
6. Check the product in Shopify admin (and the size guide on the storefront),
   then click **Load products** again. Everything you fixed should now show ✅.

---

## Editing the tables (all open in Excel)

| File | What's in it |
|---|---|
| `config/product_rules.csv` | Title words → type, category, HS code, weight. Checked top to bottom, first match wins (so "bundle" comes before "vinyl" and "beanie" before "hat"). Words are separated by `\|`. Still blank: HS code for cassette and box set, and weights for long sleeve and tote. |
| `config/size_guides.csv` | Your size guides: one row per blank + measurement, one column per size. To add a blank, add its rows. |
| `config/stores.csv` | Stores for the dropdown. |
| `config/settings.json` | Preorder tags, release-date words, size guide format, field overrides. |

## Files

- `app.py`: the screens
- `rules.py`: the checks and suggestions
- `shopify_client.py`: reading from and saving to Shopify
- `tests/`: run `python -m pytest tests` to check nothing broke after changes
