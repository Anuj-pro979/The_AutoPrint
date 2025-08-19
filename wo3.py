"""
Streamlit sender for the Firestore-based receiver (gui_auto_print_json_production_v2_firestore.py).

Upgrades in this version:
- Uses `st.secrets["firebase_service_account"]` by default (or falls back to an uploaded JSON).
- Synchronous, reliable upload flow (avoids background-thread UI updates which are fragile in Streamlit).
- Batch chunk uploads (to respect Firestore write limits). Commits batches of <=400 writes.
- Safer default chunk size and enforced maximum to avoid Firestore doc size limits.
- Handles service-account stored as JSON string or nested dict in `st.secrets`.
- Adds compression flag in manifest.
- Improved error handling and logging.

SECURITY NOTE: For production, never place service account credentials in a client app. Use a secure server-side upload or authenticated Firestore client with rules.

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
import os
from datetime import datetime

# firebase-admin imports
import firebase_admin
from firebase_admin import credentials, firestore

# ----------------- Helpers -----------------

def init_firestore_from_dict_or_string(sa_value):
    """
    Initialize firebase_admin using a dict or JSON string from st.secrets.
    Returns Firestore client.
    """
    try:
        if isinstance(sa_value, str):
            sa_dict = json.loads(sa_value)
        elif isinstance(sa_value, dict):
            sa_dict = sa_value
        else:
            raise RuntimeError("Unsupported service account format in st.secrets['firebase_service_account']")

        # Ensure private key newlines are correct
        if 'private_key' in sa_dict and isinstance(sa_dict['private_key'], str):
            sa_dict['private_key'] = sa_dict['private_key'].replace('\n', '
')

        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(sa_dict)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        raise RuntimeError(f"Failed to init Firestore from st.secrets: {e}")


def init_firestore_from_uploaded_file(uploaded_file):
    try:
        sa_bytes = uploaded_file.read()
        sa_dict = json.loads(sa_bytes.decode('utf-8'))
        if 'private_key' in sa_dict and isinstance(sa_dict['private_key'], str):
            sa_dict['private_key'] = sa_dict['private_key'].replace('\n', '
')
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(sa_dict)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        raise RuntimeError(f"Failed to initialize Firestore from uploaded file: {e}")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def compress_if_needed(b: bytes, do_compress: bool):
    if do_compress:
        return zlib.compress(b)
    return b


def split_base64_into_parts(b64_full: str, chunk_size_chars: int):
    total = len(b64_full)
    parts = [b64_full[i:i+chunk_size_chars] for i in range(0, total, chunk_size_chars)]
    return parts


def upload_chunks_in_batches(db, collection: str, file_id: str, parts: list, log_fn=None, batch_size=400):
    """
    Upload chunk docs in batches to respect Firestore limits. Returns total_chunks.
    Each doc: id "{file_id}_{idx}" with fields {"chunk_index": idx, "data": part}
    """
    total_chunks = len(parts)
    idx = 0
    while idx < total_chunks:
        batch = db.batch()
        end = min(idx + batch_size, total_chunks)
        for i in range(idx, end):
            doc_ref = db.collection(collection).document(f"{file_id}_{i}")
            batch.set(doc_ref, {"chunk_index": i, "data": parts[i]})
        try:
            batch.commit()
            if log_fn:
                log_fn(f"Committed chunks {idx}..{end-1} in a batch.")
        except Exception as e:
            if log_fn:
                log_fn(f"Batch commit failed at chunks {idx}..{end-1}: {e}")
            raise
        idx = end
    return total_chunks


def create_or_update_manifest(db, collection: str, file_id: str, manifest: dict):
    meta_doc_id = f"{file_id}_meta"
    db.collection(collection).document(meta_doc_id).set(manifest)
    return manifest


def pretty_ts(x):
    try:
        if not x:
            return "N/A"
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(int(x)).strftime("%Y-%m-%d %H:%M:%S")
        return str(x)
    except Exception:
        return str(x)


# ----------------- Streamlit UI -----------------

st.set_page_config(page_title="Firestore File Sender", layout="wide")
st.title("Firestore File Sender — upgraded (uses st.secrets)")

st.info("This sender uploads files as chunk documents + manifest to Firestore. Prefer using st.secrets['firebase_service_account'] for local testing. Do not expose service accounts in production.")

# Sidebar: Firestore & options
with st.sidebar:
    st.header("Connection & options")
    use_secrets = "firebase_service_account" in st.secrets
    if use_secrets:
        st.success("Using service account from st.secrets['firebase_service_account']")
    else:
        st.warning("No service account found in st.secrets. You can upload one below for testing (not recommended for production).")

    sa_upload = None
    if not use_secrets:
        sa_upload = st.file_uploader("Upload Firebase service account JSON (required if not using st.secrets)", type=["json"])  # fallback

    collection = st.text_input("Firestore collection", value="files")

    # Chunk size: keep safe defaults to avoid exceeding Firestore doc size limit (~1MB per document)
    st.markdown("**Chunk size (KB)** — recommended: 64..256 KB. Keep <= 300 for safety.")
    chunk_kb = st.number_input("Chunk size (KB)", min_value=16, max_value=300, value=150, step=8)

    compress = st.checkbox("Compress payload with zlib", value=True)
    create_manifest_first = st.checkbox("Create manifest BEFORE chunks (receiver will wait and fetch chunks)", value=True)

    st.markdown("---")
    st.markdown("**Sender identity (will be placed into manifest)**")
    user_name = st.text_input("User name", value="StreamlitUser")
    user_id = st.text_input("User id", value=str(uuid.uuid4()))
    user_email = st.text_input("User email (optional)")

    st.markdown("---")
    st.markdown("**Print settings (manifest.settings)**")
    copies = st.number_input("Copies", min_value=1, max_value=100, value=1)
    color_mode = st.selectbox("Color mode", options=["bw", "color"], index=0)
    duplex = st.selectbox("Duplex", options=["one-sided", "two-sided"], index=0)
    printerName = st.text_input("Preferred printer name (optional)")

# Main area: file upload
uploaded_files = st.file_uploader("Select file(s) to send", accept_multiple_files=True)

if 'sent_ids' not in st.session_state:
    st.session_state['sent_ids'] = []

# Initialize Firestore client (prefer st.secrets)
if "firebase_service_account" in st.secrets:
    try:
        db = init_firestore_from_dict_or_string(st.secrets["firebase_service_account"])
    except Exception as e:
        st.error(f"Failed to init Firestore from st.secrets: {e}")
        st.stop()
else:
    if sa_upload is None:
        st.warning("Provide a service account JSON in the sidebar or set st.secrets['firebase_service_account'].")
        db = None
    else:
        try:
            db = init_firestore_from_uploaded_file(sa_upload)
        except Exception as e:
            st.error(str(e))
            st.stop()

if db is None:
    st.stop()


if uploaded_files:
    for f in uploaded_files:
        st.write(f"**File:** {f.name} — {int(f.size/1024)} KB")
        with st.expander(f"Send options — {f.name}"):
            if st.button(f"Send '{f.name}' now", key=f"send_{f.name}_{f.size}"):
                try:
                    with st.spinner("Preparing file and uploading..."):
                        raw = f.read()
                        sha = sha256_hex(raw)
                        compressed = compress_if_needed(raw, compress)
                        compressed_flag = compress
                        b64 = base64.b64encode(compressed).decode('ascii')

                        # chunk size in characters
                        chunk_size_chars = int(chunk_kb) * 1024
                        # create file_id
                        file_id = uuid.uuid4().hex

                        settings = {
                            "copies": int(copies),
                            "colorMode": color_mode,
                            "duplex": duplex,
                            "printerName": printerName
                        }
                        user_meta = {"name": user_name, "id": user_id}
                        if user_email:
                            user_meta["email"] = user_email

                        # create manifest first if requested (with total_chunks=0 placeholder)
                        meta_doc_id = f"{file_id}_meta"
                        if create_manifest_first:
                            initial_manifest = {
                                "file_name": f.name,
                                "total_chunks": 0,
                                "sha256": sha,
                                "settings": settings,
                                "user": user_meta,
                                "timestamp": int(time.time()),
                                "compression": "zlib" if compressed_flag else "none"
                            }
                            create_or_update_manifest(db, collection, file_id, initial_manifest)

                        # Prepare parts and upload in batches
                        parts = split_base64_into_parts(b64, chunk_size_chars)
                        # function to log into a small area
                        log_area = st.empty()
                        def log(msg):
                            log_area.text(msg)

                        total_chunks = upload_chunks_in_batches(db, collection, file_id, parts, log_fn=log, batch_size=400)

                        # finalize manifest with total_chunks
                        manifest = {
                            "file_name": f.name,
                            "total_chunks": int(total_chunks),
                            "sha256": sha,
                            "settings": settings,
                            "user": user_meta,
                            "timestamp": int(time.time()),
                            "compression": "zlib" if compressed_flag else "none"
                        }
                        create_or_update_manifest(db, collection, file_id, manifest)

                        st.success(f"Upload complete for {f.name}. file_id={file_id}, chunks={total_chunks}")
                        st.session_state['sent_ids'].append({"file_id": file_id, "file_name": f.name})
                except Exception as e:
                    st.error(f"Upload failed: {e}")

# Status area: list sent ids and provide refresh
st.markdown("---")
st.subheader("Sent files / check status")
if st.session_state['sent_ids']:
    for info in st.session_state['sent_ids']:
        cols = st.columns([1,4,2,2])
        cols[0].write(info['file_id'][:8])
        cols[1].write(info['file_name'])
        if cols[2].button(f"Refresh {info['file_id'][:8]}", key=f"refresh_{info['file_id']}"):
            try:
                meta_ref = db.collection(collection).document(f"{info['file_id']}_meta")
                meta = meta_ref.get()
                if not meta.exists:
                    st.warning("Manifest not found yet (receiver may not have created payinfo)")
                else:
                    md = meta.to_dict()
                    st.json(md)
                    payinfo = md.get('payinfo')
                    if payinfo:
                        st.success(f"Receiver payinfo: amount {payinfo.get('amount_str')} {payinfo.get('currency')} — status {payinfo.get('status')}")
                        st.write(payinfo)
                    else:
                        st.info("No payinfo yet in manifest. Receiver may be downloading/processing.")
            except Exception as e:
                st.error(f"Failed to fetch manifest: {e}")
        if cols[3].button(f"Open UPI (if present)", key=f"upi_{info['file_id']}"):
            try:
                meta = db.collection(collection).document(f"{info['file_id']}_meta").get().to_dict() or {}
                payinfo = meta.get('payinfo') or {}
                upi = payinfo.get('upi_url') or meta.get('upi_url') or None
                if upi:
                    st.write(f"UPI url: {upi}")
                else:
                    st.info("No UPI url present in manifest yet.")
            except Exception as e:
                st.error(str(e))
else:
    st.info("No files sent in this session yet.")

if st.button("Clear sent IDs"):
    st.session_state['sent_ids'] = []

st.caption("Remember: using a service account from the client is insecure. Use this only for local testing.")
