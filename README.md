inventory_snapshot.py snapshot xlwms inventory updates (1h)

# Refactored MySQL DB
Tables:
- book_sku: book sku and information

- inventory_locked: lock skan skus for inventory manual

- invenrory_snapshor: napshot xlwms inventory updates (1h)

- login_info: login information

- sku_recording: sku recorded

- sku_recordeing_log: log for change or delete

- stores: store id mapping store name

- tkorders_items_tu: 1 order ID could mapping mutil items

- tkorders_tu: orders information

- weekly_inventory: manual created sales list 

# Refactored invernory
# Files:
- `db.py` — MySQL connection (shared across scripts)
- `inventory_query.py` — the query + row shaping
- `feishu_sheet.py` — generic Feishu Sheets client (auth + write grid)
- `inventory_query_to_feishu.py` — entry point, wires the two together
# Refactored web backend

# Files:
- app.py: Flask startup / config / blueprint registration
- auth.py: login, logout, Flask-Login, user loader, last_seen
- db.py: MySQL connection and UTC session timezone
- utils.py: UTC -> Eastern display formatting
- inventory_service.py: shared weekly SKU status calculation
- recording.py: recording page and all recording APIs
- weekly_inventory.py: weekly inventory page and APIs

Keep your existing `templates/` directory unchanged next to these Python files.

Run the same way as before:

    python web/app.py

The public URL paths are unchanged.