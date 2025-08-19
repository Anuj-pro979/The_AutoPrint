"""
Streamlit sender for the Firestore-based receiver (gui_auto_print_json_production_v2_firestore.py).

This file is the **fixed, runnable** version. Primary fix: avoid any unterminated string literal when normalizing the service-account `private_key` newlines. The code uses safe escapes ("\n" → "
") and a robust initialization path.

Features:
- Uses `st.secrets['firebase_service_account']` if present (string or dict), or accepts an uploaded JSON file.
- Compresses (optional), base64-encodes, splits into chunks and uploads to Firestore as `{file_id}_{idx}` documents.
- Writes a manifest `{file_id}_meta` with `total_chunks`, `file_name`, `sha256`, `settings`, `user`, `compression`, and `timestamp`.
- Uploads chunks in batches (Firestore-friendly) with exponential backoff retries.
- Synchronous uploads with clear Streamlit progress messages.

SECURITY: Do NOT ship service account credentials in a client app in production.

Dependencies:
    pip install streamlit firebase-admin

Run:
    streamlit run streamlit_firestore_sender.py

"""

import streamlit as st
import base64
import zlib
import hashlib
import uuid
import time
import json
from datetime import datetime

# Firebase admin
import firebase_admin
from firebase_admin import credentials, firestore

# ---------------- Helpers ----------------

def retry_with_backoff(fn, max_attempts=5, initial_delay=1.0, factor=2.0, exceptions=(Exception,), log_fn=None):
    attempt = 0
    while True:
        try:
            return fn()
        except exceptions as e:
            attempt += 1
            if attempt >= max_attempts:
                raise
            delay = initial_delay * (factor ** (attempt - 1))
            if log_fn:
                try:
                    log_fn(f"Attempt {attempt}/{max_attempts} failed: {e}. Retrying in {delay:.1f}s...")
                except Exception:
                    pass
            time.sleep(delay)


def init_firestore_from_secrets_or_upload(secrets_key="firebase_service_account", uploaded_file=None):
    """
    Initialize Firestore client using st.secrets[secrets_key] (preferred) or an uploaded JSON file.
    Returns Firestore client.
    """
    sa_dict = None
    # 1) try st.secrets
    if secrets_key in st.secrets:
        candidate = st.secrets[secrets_key]
        if isinstance(candidate, str):
            try:
                sa_dict = json.loads(candidate)
            except Exception:
                # maybe it's already JSON-like string with single quotes or other formatting
                raise RuntimeError("st.secrets['firebase_service_account'] must be a JSON string or dict")
        elif isinstance(candidate, dict):
            sa_dict = candidate
        else:
            raise RuntimeError("Unsupported format for st.secrets['firebase_service_account']")

    # 2) fall back to uploaded file
    if sa_dict is None and uploaded_file is not None:
        try:
            raw = uploaded_file.read()
            sa_dict = json.loads(raw.decode('utf-8'))
        except Exception as e:
            raise RuntimeError(f"Uploaded service account JSON parse failed: {e}")

    if sa_dict is None:
        raise RuntimeError("No service account provided. Set st.secrets['firebase_service_account'] or upload a JSON file.")

    # Normalize private_key newlines safely. Use '\n' in source to represent backslash+n.
    if 'private_key' in sa_dict and isinstance(sa_dict['private_key'], str):
        # Replace literal 
 sequences with real newlines; do NOT create an unterminated string in source.
        sa_dict['private_key'] = sa_dict['private_key'].replace('\n', '
')

    # Initialize firebase_admin if not already
    try:
        firebase_admin.get_app()
    except ValueError:
        cred = credentials.Certificate(sa_dict)
        firebase_admin.initialize_app(cred)

    return firestore.client()


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def compress_if_needed(b: bytes, do_compress: bool):
    return zlib.compress(b) if do_compress else b


def split_base64_into_parts(b64_full: str, chunk_size_chars: int):
    return [b64_full[i:i+chunk_size_chars] for i in range(0, len(b64_full), chunk_size_chars)]


def upload_chunks_in_batches(db, collection: str, file_id: str, parts: list, log_fn=None, batch_size=300):
    total_chunks = len(parts)
    idx = 0
    while idx < total_chunks:
        batch = db.batch()
        end = min(idx + batch_size, total_chunks)
        for i in range(idx, end):
            doc_ref = db.collection(collection).document(f"{file_id}_{i}")
            batch.set(doc_ref, {"chunk_index": i, "data": parts[i]})
        def _commit():
            batch.commit()
            return True
        retry_with_backoff(_commit, max_attempts=6, initial_delay=1.0, factor=2.0, exceptions=(Exception,), log_fn=log_fn)
        if log_fn:
            log_fn(f"Committed chunks {idx}..{end-1}")
        idx = end
    return total_chunks


def write_manifest(db, collection: str, file_id: str, manifest: dict, log_fn=None):
    meta_doc_id = f"{file_id}_meta"
    def _set():
        db.collection(collection).document(meta_doc_id).set(manifest)
        return True
    retry_with_backoff(_set, max_attempts=6, initial_delay=1.0, factor=2.0, exceptions=(Exception,), log_fn=log_fn)
    if log_fn:
        log_fn(f"Wrote manifest {meta_doc_id}")


def pretty_ts(x):
    try:
        if not x:
            return "N/A"
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(int(x)).strftime("%Y-%m-%d %H:%M:%S")
        return str(x)
    except Exception:
        return str(x)

# ---------------- Streamlit UI ----------------

st.set_page_config(page_title="Firestore File Sender (fixed)", layout="wide")
st.title("Firestore File Sender — fixed syntax & robust init")

st.info("Use st.secrets['firebase_service_account'] (preferred) or upload a service-account JSON. This tool is for local testing only.")

with st.sidebar:
    st.header("Connection & options")
    use_secrets = "firebase_service_account" in st.secrets
    if use_secrets:
        st.success("Using st.secrets['firebase_service_account']")
    else:
        st.warning("No service account in st.secrets — you may upload a JSON here for testing (not for production)")
    sa_upload = st.file_uploader("Upload service-account JSON (fallback)", type=["json"]) if not use_secrets else None
    collection = st.text_input("Firestore collection", value="files")
    st.markdown("---")
    st.markdown("**Chunking & compression**")
    chunk_kb = st.number_input("Chunk size (KB)", min_value=16, max_value=256, value=128, step=8)
    compress = st.checkbox("Compress payload with zlib", value=True)
    create_manifest_first = st.checkbox("Create manifest BEFORE chunks", value=True)
    st.markdown("---")
    st.markdown("**Sender identity**")
    user_name = st.text_input("User name", value="StreamlitUser")
    user_id = st.text_input("User id", value=str(uuid.uuid4()))
    user_email = st.text_input("User email (optional)")
    st.markdown("---")
    st.markdown("**Print settings**")
    copies = st.number_input("Copies", min_value=1, max_value=100, value=1)
    color_mode = st.selectbox("Color mode", options=["bw", "color"], index=0)
    duplex = st.selectbox("Duplex", options=["one-sided", "two-sided"], index=0)
    printerName = st.text_input("Preferred printer name (optional)")

uploaded_files = st.file_uploader("Select file(s) to send", accept_multiple_files=True)

if 'sent_ids' not in st.session_state:
    st.session_state['sent_ids'] = []

# Initialize Firestore
try:
    db = init_firestore_from_secrets_or_upload(secrets_key="firebase_service_account", uploaded_file=sa_upload)
except Exception as e:
    st.error(f"Firestore init failed: {e}")
    st.stop()

if uploaded_files:
    for f in uploaded_files:
        st.write(f"**File:** {f.name} — {int(f.size/1024)} KB")
        with st.expander(f"Send options — {f.name}"):
            if st.button(f"Send '{f.name}' now", key=f"send_{f.name}_{f.size}"):
                try:
                    with st.spinner("Uploading..."):
                        raw = f.read()
                        sha = sha256_hex(raw)
                        compressed = compress_if_needed(raw, compress)
                        compressed_flag = compress
                        b64 = base64.b64encode(compressed).decode('ascii')

                        chunk_size_chars = int(chunk_kb) * 1024
                        file_id = uuid.uuid4().hex

                        settings = {"copies": int(copies), "colorMode": color_mode, "duplex": duplex, "printerName": printerName}
                        user_meta = {"name": user_name, "id": user_id}
                        if user_email:
                            user_meta['email'] = user_email

                        if create_manifest_first:
                            initial_manifest = {"file_name": f.name, "total_chunks": 0, "sha256": sha, "settings": settings, "user": user_meta, "timestamp": int(time.time()), "compression": "zlib" if compressed_flag else "none"}
                            write_manifest(db, collection, file_id, initial_manifest, log_fn=lambda m: None)

                        parts = split_base64_into_parts(b64, chunk_size_chars)
                        log_area = st.empty()
                        def log(msg):
                            log_area.text(msg)

                        total_chunks = upload_chunks_in_batches(db, collection, file_id, parts, log_fn=log, batch_size=300)

                        manifest = {"file_name": f.name, "total_chunks": int(total_chunks), "sha256": sha, "settings": settings, "user": user_meta, "timestamp": int(time.time()), "compression": "zlib" if compressed_flag else "none"}
                        write_manifest(db, collection, file_id, manifest, log_fn=log)

                        st.success(f"Upload complete for {f.name}. file_id={file_id}, chunks={total_chunks}")
                        st.session_state['sent_ids'].append({"file_id": file_id, "file_name": f.name})
                except Exception as e:
                    st.error(f"Upload failed: {e}")

st.markdown("---")
st.subheader("Sent files / check status")
if st.session_state['sent_ids']:
    for info in st.session_state['sent_ids']:
        cols = st.columns([1,4,2,2])
        cols[0].write(info['file_id'][:8])
        cols[1].write(info['file_name'])
        if cols[2].button(f"Refresh {info['file_id'][:8]}", key=f"refresh_{info['file_id']}"):
            try:
                meta = db.collection(collection).document(f"{info['file_id']}_meta").get()
                if not meta.exists:
                    st.warning("Manifest not found yet")
                else:
                    md = meta.to_dict()
                    st.json(md)
                    payinfo = md.get('payinfo')
                    if payinfo:
                        st.success(f"Receiver payinfo: amount {payinfo.get('amount_str')} {payinfo.get('currency')} — status {payinfo.get('status')}")
                        st.write(payinfo)
                    else:
                        st.info("No payinfo yet in manifest.")
            except Exception as e:
                st.error(f"Failed to fetch manifest: {e}")
        if cols[3].button(f"Open UPI (if present)", key=f"upi_{info['file_id']}"):
            try:
                md = db.collection(collection).document(f"{info['file_id']}_meta").get().to_dict() or {}
                payinfo = md.get('payinfo') or {}
                upi = payinfo.get('upi_url') or md.get('upi_url') or None
                if upi:
                    st.write(f"UPI url: {upi}")
                else:
                    st.info("No UPI url present yet.")
            except Exception as e:
                st.error(str(e))
else:
    st.info("No files sent in this session yet.")

if st.button("Clear sent IDs"):
    st.session_state['sent_ids'] = []

st.caption("Reminder: Do not expose service-account credentials in a client app for production.")
