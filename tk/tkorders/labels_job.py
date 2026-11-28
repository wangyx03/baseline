"""
Background jobs that collect TikTok shipping labels (or labels + packing slips) for many
packages and merge them into one PDF.

Why a job: each package is one TikTok API call plus one PDF download (~1 s), so a few
hundred packages would exceed the proxy timeout (504). The page starts a job, polls its
progress and downloads the merged PDF when it is done.

Job state lives in files under JOB_ROOT, so every gunicorn worker can answer the polls:
    JOB_ROOT/<job_id>/status.json   progress + failures
    JOB_ROOT/<job_id>/labels.pdf    merged result
Jobs older than JOB_TTL_HOURS are removed when a new job starts.
"""
import io
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from api.tkapi.client import SHOPS, TikTokAPIError
from tk.tkorders.shipping_docs import get_document_url

JOB_ROOT = Path(os.environ.get('TK_LABEL_JOB_DIR', '/tmp/tk_label_jobs'))
JOB_TTL_HOURS = 24
WORKERS = 4              # parallel TikTok calls per job (keeps well under the API rate limit)
MAX_PACKAGES = 2000
DOC_TYPES = {'label': 'SHIPPING_LABEL', 'both': 'SHIPPING_LABEL_AND_PACKING_SLIP'}
DOC_SIZE = os.environ.get('TK_LABEL_SIZE', 'A6')


def _job_dir(job_id):
    if not job_id or not all(c in '0123456789abcdef' for c in job_id):
        raise ValueError('bad job id')
    return JOB_ROOT / job_id


def _write_status(job_dir, status):
    tmp = job_dir / 'status.json.tmp'
    tmp.write_text(json.dumps(status), encoding='utf-8')
    os.replace(tmp, job_dir / 'status.json')


def status(job_id):
    path = _job_dir(job_id) / 'status.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def result_path(job_id):
    path = _job_dir(job_id) / 'labels.pdf'
    return path if path.exists() else None


def _cleanup():
    if not JOB_ROOT.exists():
        return
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    for d in JOB_ROOT.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def start(packages, kind):
    """packages: [{'store_id', 'tracking', 'package_ids': [...]}] in print order.
    kind: 'label' | 'both'. Returns the job id."""
    if kind not in DOC_TYPES:
        raise ValueError('kind must be label or both')
    _cleanup()
    job_id = uuid.uuid4().hex
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True)
    _write_status(job_dir, {'state': 'running', 'kind': kind, 'total': len(packages),
                            'done': 0, 'failed': [], 'pages': 0, 'error': None})
    threading.Thread(target=_run, args=(job_dir, packages, kind), daemon=True).start()
    return job_id


def fetch(pkg, kind):
    """-> (list of PDF bytes, error text or None) for one package (one tracking)."""
    shop = SHOPS.get(int(pkg['store_id']))
    if not shop:
        return [], 'unknown store'
    if not pkg['package_ids']:
        return [], 'no package_id (order not synced yet)'
    pdfs = []
    for package_id in pkg['package_ids']:
        try:
            doc = get_document_url(shop, package_id, DOC_TYPES[kind], DOC_SIZE)
        except TikTokAPIError as exc:
            return pdfs, f'TikTok: {exc}'.replace(f"[{shop['short_name']}] ", '')
        if not doc.get('doc_url'):
            return pdfs, 'TikTok returned no document (label not generated yet?)'
        try:
            resp = requests.get(doc['doc_url'], timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            return pdfs, f'download failed: {type(exc).__name__}'
        pdfs.append(resp.content)
    return pdfs, None


def fetch_one_pdf(pkg, kind='label'):
    """-> (one PDF as bytes or None, error text or None). Several package_ids under one
    tracking (rare) are merged into one file."""
    pdfs, err = fetch(pkg, kind)
    if err:
        return None, err
    if len(pdfs) == 1:
        return pdfs[0], None
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for data in pdfs:
        for page in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), None


def fetch_many(packages, kind='label'):
    """{(store_id, tracking): (pdf bytes or None, error or None)}, WORKERS at a time."""
    out = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_one_pdf, p, kind): (int(p['store_id']), p['tracking']) for p in packages}
        for fut in as_completed(futures):
            try:
                out[futures[fut]] = fut.result()
            except Exception as exc:
                out[futures[fut]] = (None, f'{type(exc).__name__}: {exc}')
    return out


def _run(job_dir, packages, kind):
    from pypdf import PdfReader, PdfWriter
    st = json.loads((job_dir / 'status.json').read_text(encoding='utf-8'))
    results = [None] * len(packages)
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(fetch, p, kind): i for i, p in enumerate(packages)}
            done = 0
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:          # never let one package kill the job
                    results[i] = ([], f'{type(exc).__name__}: {exc}')
                done += 1
                if done % 5 == 0 or done == len(packages):
                    st['done'] = done
                    _write_status(job_dir, st)
        writer = PdfWriter()
        failed = []
        for pkg, (pdfs, err) in zip(packages, results):
            for data in pdfs:
                for page in PdfReader(io.BytesIO(data)).pages:
                    writer.add_page(page)
            if err:
                failed.append({'store': 'TU' if int(pkg['store_id']) == 1 else 'VB',
                               'tracking': pkg['tracking'], 'error': err[:300]})
        st['failed'] = failed
        st['pages'] = len(writer.pages)
        if writer.pages:
            with open(job_dir / 'labels.pdf', 'wb') as f:
                writer.write(f)
        st['state'] = 'done'
    except Exception as exc:
        st['state'] = 'error'
        st['error'] = f'{type(exc).__name__}: {exc}'
    _write_status(job_dir, st)
