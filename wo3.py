import streamlit as st
import base64
import zlib
import hashlib
import uuid
import time
import json
from datetime import datetime

# firebase-admin
import firebase_admin
from firebase_admin import credentials, firestore

# streamlit components
import streamlit.components.v1 as components

# ---------------- Helpers (from your original sender app) ----------------

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


def init_firestore_from_uploaded_file(uploaded_file):
    if uploaded_file is None:
        raise RuntimeError("Service account JSON must be uploaded in the sidebar.")
    try:
        raw = uploaded_file.read()
        sa_dict = json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise RuntimeError(f"Failed to parse uploaded service-account JSON: {e}")

    # NOTE: do not strip real newlines. Convert literal backslash-n to newline if present.
    if 'private_key' in sa_dict and isinstance(sa_dict['private_key'], str):
        if '\\n' in sa_dict['private_key'] and '\n' not in sa_dict['private_key']:
            sa_dict['private_key'] = sa_dict['private_key'].replace('\\n', '\n')

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
    return [b64_full[i:i + chunk_size_chars] for i in range(0, len(b64_full), chunk_size_chars)]


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
            log_fn(f"Committed chunks {idx}..{end - 1}")
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


# ---------------- Streamlit UI + Editor integration ----------------

st.set_page_config(page_title="Firestore File Sender + Editor", layout="wide")
st.title("Firestore File Sender — with client-side PDF editor integration")

st.info("This app allows uploading files to Firestore and previewing/editing PDFs client-side using an external editor popup.\n\nNote: due to Streamlit components limitations, large edited binary data cannot be automatically sent from the browser iframe back to Python without a custom Streamlit component or a backend endpoint. See the 'How to persist edits' section below.")

with st.sidebar:
    st.header("Connection & options")
    sa_upload = st.file_uploader("Upload service-account JSON (required)", type=["json"])
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

# initialize session state containers
if 'sent_ids' not in st.session_state:
    st.session_state['sent_ids'] = []
if 'files_data' not in st.session_state:
    st.session_state['files_data'] = {}

# Try to init Firestore
try:
    db = init_firestore_from_uploaded_file(sa_upload)
except Exception as e:
    st.error(f"Firestore init failed: {e}")
    db = None

# File uploader (same as your editor app) — these files will be available for preview/edit
uploaded_files = st.file_uploader("Select file(s) to send / edit", accept_multiple_files=True)

if uploaded_files:
    for f in uploaded_files:
        if f.name not in st.session_state['files_data']:
            raw = f.read()
            b64 = base64.b64encode(raw).decode('ascii')
            st.session_state['files_data'][f.name] = {
                'original_base64': b64,
                'current_base64': b64,
                'original_bytes': raw,
                'current_bytes': raw,
                'edited': False,
                'uploaded_at': int(time.time() * 1000),
                # a place to store a linked Firestore file_id if we uploaded it
                'file_id': None,
            }

# Show files and interactive cards (includes editor popup integration code adapted from your editor app)
if not st.session_state['files_data']:
    st.info("No files uploaded yet — use the uploader above to add files.")
else:
    st.write(f"{len(st.session_state['files_data'])} file(s) ready")

    for filename, fd in st.session_state['files_data'].items():
        file_key = filename.replace('.', '_').replace(' ', '_').replace('-', '_')
        EDITOR_URL = "https://anuj-pro979.github.io/printdilog/"  # external JS-based editor

        # IMPORTANT: compute origin dynamically in JS from the Editor URL to avoid mismatch
        js_editor_url = json.dumps(EDITOR_URL)
        js_file_key = json.dumps(file_key)
        js_filename = json.dumps(filename)
        # embed base64 only for moderate-size files; watch out for very large files
        js_base64 = json.dumps(fd['current_base64'])

        uploaded_ts = fd.get('uploaded_at', 0)
        uploaded_time_str = time.strftime('%d %b %Y, %H:%M', time.localtime(uploaded_ts/1000)) if uploaded_ts else ""
        file_size_mb = round(len(fd['current_bytes']) / (1024 * 1024), 2) if fd['current_bytes'] else 0

        # Build html as a plain string and inject variables with % formatting (avoids f-string brace issues)
        html = """
<div class="file-container" style="padding:10px; border-radius:8px; border:1px solid #ddd; margin-bottom:12px;">
    <div style="display:flex; align-items:center; justify-content:space-between">
      <div>
        <!-- Give the title an explicit id so JS can update it -->
        <div id="title_%s" style="font-weight:700">%s</div>
        <div style="font-size:12px; color:#666">%s • %s MB • %s bytes</div>
      </div>
      <div>
        <button id="edit_%s" style="padding:8px 12px; margin-right:8px;">✏️ Preview & Edit</button>
        <button id="dl_%s" style="padding:8px 12px;">⬇️ Download</button>
      </div>
    </div>

<script>
(function(){
  const EDITOR_URL = %s;
  // compute origin dynamically to avoid mismatches
  const TARGET_ORIGIN = (new URL(EDITOR_URL)).origin;
  const fileKey = %s;
  let filename = %s;
  let currentBase64 = %s;
  let popup = null;
  let popupReady = false;
  let lastBlobUrl = null;

  function log(...args) {
    try { console.log("[file-card]", fileKey, ...args); } catch(e){}
  }

  function base64ToUint8Array(b64) {
    const bin = atob(b64);
    const len = bin.length;
    const arr = new Uint8Array(len);
    for (let i = 0; i < len; i++) arr[i] = bin.charCodeAt(i);
    return arr;
  }

  function makeBlobUrlFromBase64(b64) {
    if (lastBlobUrl) { URL.revokeObjectURL(lastBlobUrl); lastBlobUrl = null; }
    const uint8 = base64ToUint8Array(b64);
    const blob = new Blob([uint8], { type: "application/pdf" });
    const url = URL.createObjectURL(blob);
    lastBlobUrl = url;
    return url;
  }

  const downloadBtn = document.getElementById("dl_" + fileKey);
  downloadBtn.onclick = () => {
    try {
      const url = makeBlobUrlFromBase64(currentBase64);
      const a = document.createElement("a");
      a.href = url;
      let dlName = filename;
      if (!/\.pdf$/i.test(dlName)) dlName = dlName + ".pdf";
      a.download = dlName;
      a.click();
      log("Download triggered for", dlName);
    } catch(e) {
      console.error("Download error", e);
      alert("Download failed: " + (e && e.message ? e.message : e));
    }
  };

  function openPopup() {
    try {
      // try opening via the top window to reduce popup blocking risk
      const openerWindow = (window.top && window.top !== window) ? window.top : window;
      popup = openerWindow.open(EDITOR_URL, "editor_popup_" + fileKey, "width=1200,height=800,resizable,scrollbars");
      if (!popup) {
        alert("Popup blocked. Please allow popups for this site or try opening the editor in a new tab manually.");
        log("Popup blocked");
        return;
      }
      popupReady = false;

      // ping the popup until it responds with pdf_editor_ready
      let attempts = 0;
      const pingInterval = setInterval(() => {
        if (!popup || popup.closed) { clearInterval(pingInterval); return; }
        attempts++;
        try {
          // We postMessage directly to the popup, trusting its origin (computed above)
          popup.postMessage({type:'ping', from:'sender'}, TARGET_ORIGIN);
        } catch(e) {
          // ignore
        }
        if (attempts > 120) {
          clearInterval(pingInterval);
          log("Giving up pinging popup after attempts", attempts);
        }
      }, 300);
    } catch(e) {
      console.error("openPopup error", e);
      alert("Could not open editor popup: " + e.message);
    }
  }

  function sendFile() {
    if (!popup || popup.closed) return;
    if (!popupReady) return;
    try {
      popup.postMessage({ type: "pdf_file_data", filename: filename, pdf_data: currentBase64 }, TARGET_ORIGIN);
      log("Sent file data to popup for", filename);
    } catch(e) {
      console.error("sendFile error", e);
    }
  }

  document.getElementById("edit_" + fileKey).addEventListener("click", openPopup);

  // Listen for messages coming back FROM the editor popup
  window.addEventListener("message", (event) => {
    try {
      // Only accept messages from the editor origin for safety
      if (!event.origin || event.origin !== TARGET_ORIGIN) return;
      const data = event.data || {};
      // Accept messages only from the popup window we opened
      if (event.source !== popup) {
        // However, in some browsers popup.opener may be the top window — so allow messages whose
        // data contains our known message types as a fallback.
        if (!data || !data.type || (data.type !== "pdf_editor_ready" && data.type !== "pdf_edited_data")) {
          return;
        }
      }

      if (data.type === "pdf_editor_ready") {
        popupReady = true;
        log("popup ready");
        sendFile();
      }

      if (data.type === "pdf_edited_data") {
        const editedBase64 = data.pdf_data;
        const editedNameFromEditor = data.filename || filename;
        const ts = Date.now();
        const newName = (editedNameFromEditor.replace(/\\.pdf$/i, "") || filename.replace(/\\.pdf$/i,"")) + "_edited_" + ts + ".pdf";

        currentBase64 = editedBase64;
        filename = newName;

        // inform the parent Streamlit page (best-effort). NOTE: Streamlit's Python runtime won't automatically receive
        // this message unless you implement a proper Streamlit custom component or backend endpoint. This postMessage
        // is included for environments that can listen to window.message.
        try { window.parent.postMessage({ type: 'pdf_edited_data_for_streamlit', fileKey: fileKey, filename: filename, pdf_data: editedBase64 }, '*'); } catch(e){}

        // Update the visible label in this card
        try {
          const titleEl = document.getElementById("title_" + fileKey);
          if (titleEl) titleEl.textContent = filename;
        } catch(e){}

        alert('Edited file received in the popup. You can now download it (Download button) or re-upload the edited file to send it to Firestore.');
        log("Received edited data for", filename);
      }
    } catch(e) {
      console.error("message handler error", e);
    }
  });
})();
</script>
</div>
""" % (
            file_key,             # for title id
            filename,             # visible title text inside card
            uploaded_time_str,    # upload time text
            file_size_mb,         # size MB
            len(fd['current_bytes']) if fd['current_bytes'] else 0,  # bytes count
            file_key,             # edit button id
            file_key,             # download button id
            js_editor_url,        # EDITOR_URL injected into JS
            js_file_key,          # fileKey JSON value
            js_filename,          # filename JSON value
            js_base64             # base64 JSON value
        )

        # render HTML card
        components.html(html, height=180, scrolling=False)

        # Server-side action buttons (these act on the Python-side copy only)
        cols = st.columns([1, 1, 1, 3])
        if cols[0].button(f"Send '{filename}' now", key=f"send_{filename}"):
            # This will send whatever Python knows (fd['current_bytes']) to Firestore.
            # NOTE: If edits were only made client-side (inside the editor popup), Python will not yet know about them
            # unless you implement a mechanism to transfer edited base64 back to Python (see notes below).
            try:
                raw = fd['current_bytes']
                sha = sha256_hex(raw)
                compressed = compress_if_needed(raw, compress)
                compressed_flag = compress
                b64 = base64.b64encode(compressed).decode('ascii')

                chunk_size_chars = int(chunk_kb) * 1024
                file_id = uuid.uuid4().hex

                settings = {
                    "copies": int(copies),
                    "colorMode": color_mode,
                    "duplex": duplex,
                    "printerName": printerName,
                }
                user_meta = {"name": user_name, "id": user_id}
                if user_email:
                    user_meta['email'] = user_email

                if create_manifest_first:
                    initial_manifest = {
                        "file_name": filename,
                        "total_chunks": 0,
                        "sha256": sha,
                        "settings": settings,
                        "user": user_meta,
                        "timestamp": int(time.time()),
                        "compression": "zlib" if compressed_flag else "none",
                    }
                    write_manifest(db, collection, file_id, initial_manifest, log_fn=lambda m: None)

                parts = split_base64_into_parts(b64, chunk_size_chars)
                log_area = st.empty()

                def log(msg):
                    log_area.text(msg)

                total_chunks = upload_chunks_in_batches(db, collection, file_id, parts, log_fn=log, batch_size=300)

                manifest = {
                    "file_name": filename,
                    "total_chunks": int(total_chunks),
                    "sha256": sha,
                    "settings": settings,
                    "user": user_meta,
                    "timestamp": int(time.time()),
                    "compression": "zlib" if compressed_flag else "none",
                }
                write_manifest(db, collection, file_id, manifest, log_fn=log)

                st.success(f"Upload complete for {filename}. file_id={file_id}, chunks={total_chunks}")
                st.session_state['sent_ids'].append({"file_id": file_id, "file_name": filename})
                # record file_id for this card
                st.session_state['files_data'][filename]['file_id'] = file_id
            except Exception as e:
                st.error(f"Upload failed: {e}")

        if cols[1].button(f"Mark as edited (server)", key=f"mark_edited_{filename}"):
            # This button simply toggles the edited flag server-side. It does NOT magically receive edits from the popup.
            st.session_state['files_data'][filename]['edited'] = True
            st.success("Marked as edited (server-side flag only). To persist edited bytes you must re-upload the edited file.")

        if cols[2].button(f"Download current (server)", key=f"dl_server_{filename}"):
            st.download_button("Download PDF", data=fd['current_bytes'], file_name=filename, mime="application/pdf")

        cols[3].caption("If you edited the file inside the popup, either: 1) click Download and re-upload the edited file to the uploader then Send; or 2) build a backend endpoint / Streamlit custom component to accept the edited base64 and call the upload functions server-side.")

    st.markdown("---")
    st.markdown("### How to persist edits automatically (options)")
    st.markdown("1. Quick manual flow: Edit in popup → click the Download button on the card → Re-upload the resulting file in the uploader → Click Send to upload to Firestore.")
    st.markdown("2. Automated: Build a tiny server endpoint or a Streamlit custom component that accepts edited base64 (POST) from the browser and on the server calls the functions used above (split into chunks, write to Firestore). I can help implement that if you want.")

# Sent files / status area (copied from sender app)
st.markdown("---")
st.subheader("Sent files / check status")
if st.session_state['sent_ids']:
    for info in st.session_state['sent_ids']:
        cols = st.columns([1, 4, 2, 2])
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
                    cols[3].write(f"UPI url: {upi}")
                else:
                    cols[3].info("No UPI url present yet.")
            except Exception as e:
                cols[3].error(str(e))
else:
    st.info("No files sent in this session yet.")

if st.button("Clear sent IDs"):
    st.session_state['sent_ids'] = []

st.caption("Reminder: Do not expose service-account credentials in a client app for production.")
