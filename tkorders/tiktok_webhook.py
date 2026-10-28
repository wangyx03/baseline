#!/usr/bin/env python3
"""TikTok Shop webhook receiver; persist first, acknowledge promptly.
Run with: gunicorn -w 2 -b 127.0.0.1:8505 tiktok_webhook:app
Worker: python tiktokorders.py --webhook-worker
"""
import hashlib
import hmac
import json
import logging
import sys
from pathlib import Path
from flask import Flask, request, jsonify
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tiktokorders as sync
import tkorders_db as db

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024


def verify(shop, raw, signature):
    if not signature or not shop['app_key'] or not shop['app_secret']:
        return False
    expected = hmac.new(shop['app_secret'].encode(), shop['app_key'].encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def accept(store):
    shop = sync.resolve_shop(store)
    raw = request.get_data(cache=True)
    if not verify(shop, raw, request.headers.get('Authorization', '')):
        return jsonify(error='Invalid signature'), 401
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError('expected JSON object')
    except (ValueError, UnicodeDecodeError):
        return jsonify(error='Invalid JSON'), 400
    # Store entire verified notification, including non-order events, for audit.
    try:
        conn = db.get_connection()
        try:
            db.init_webhook_schema(conn)
            db.enqueue_webhook(conn, shop['store_id'], event)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        app.logger.exception('Webhook persistence failed for %s', store)
        return jsonify(error='Temporary error'), 503
    return jsonify(success=True), 200


@app.post('/tiktok/tu')
def tu():
    return accept('TU')


@app.post('/tiktok/vb')
def vb():
    return accept('VB')


@app.get('/health')
def health():
    return jsonify(status='ok', service='tiktok-webhook')
