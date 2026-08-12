# Refactored web backend

Files:
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
