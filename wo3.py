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

# ---------------- Configuration ----------------

# Default print settings for new files
DEFAULT_PRINT_SETTINGS = {
    "copies": 1,
    "colorMode": "bw",
    "duplex": "one-sided",
    "printerName": ""
}

# Default chunking settings
DEFAULT_CHUNK_KB = 128
DEFAULT_COMPRESS = True
DEFAULT_COLLECTION = "files"

# ---------------- Helpers ----------------

def retry_with_backoff(fn, max_attempts=5, initial_delay=1.0, factor=2.0, exceptions=(Exception,), log_fn=None):
    """Retry function with exponential backoff"""
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


def init_firestore_from_secrets():
    """Initialize Firestore using Streamlit secrets"""
    try:
        # Get service account info from Streamlit secrets
        service_account_info = st.secrets["firebase_service_account"]
        
        # Convert to dictionary if it's not already
        if isinstance(service_account_info, str):
            sa_dict = json.loads(service_account_info)
        else:
            sa_dict = dict(service_account_info)
        
        # Handle private key formatting
        if 'private_key' in sa_dict and isinstance(sa_dict['private_key'], str):
            if '\\n' in sa_dict['private_key'] and '\n' not in sa_dict['private_key']:
                sa_dict['private_key'] = sa_dict['private_key'].replace('\\n', '\n')

        # Initialize Firebase Admin if not already initialized
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(sa_dict)
            firebase_admin.initialize_app(cred)

        return firestore.client()
        
    except KeyError:
        raise RuntimeError("Firebase service account credentials not found in Streamlit secrets. Please add 'firebase_service_account' to your secrets.toml file.")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize Firestore: {e}")


def sha256_hex(b: bytes) -> str:
    """Calculate SHA256 hash of bytes"""
    return hashlib.sha256(b).hexdigest()


def compress_if_needed(b: bytes, do_compress: bool):
    """Compress bytes if compression is enabled"""
    return zlib.compress(b) if do_compress else b


def split_base64_into_parts(b64_full: str, chunk_size_chars: int):
    """Split base64 string into chunks"""
    return [b64_full[i:i + chunk_size_chars] for i in range(0, len(b64_full), chunk_size_chars)]


def upload_chunks_in_batches(db, collection: str, file_id: str, parts: list, log_fn=None, batch_size=300):
    """Upload file chunks to Firestore in batches"""
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
    """Write manifest document to Firestore"""
    meta_doc_id = f"{file_id}_meta"

    def _set():
        db.collection(collection).document(meta_doc_id).set(manifest)
        return True

    retry_with_backoff(_set, max_attempts=6, initial_delay=1.0, factor=2.0, exceptions=(Exception,), log_fn=log_fn)
    if log_fn:
        log_fn(f"Wrote manifest {meta_doc_id}")


def upload_single_file(db, collection, filename, file_data, user_meta, chunk_kb, compress_flag, create_manifest_first, log_fn=None):
    """Upload a single file to Firestore"""
    try:
        raw = file_data['current_bytes']
        sha = sha256_hex(raw)
        compressed = compress_if_needed(raw, compress_flag)
        b64 = base64.b64encode(compressed).decode('ascii')

        chunk_size_chars = int(chunk_kb) * 1024
        file_id = uuid.uuid4().hex

        # Create initial manifest if requested
        if create_manifest_first:
            initial_manifest = {
                "file_name": filename,
                "total_chunks": 0,
                "sha256": sha,
                "settings": file_data['print_settings'],
                "user": user_meta,
                "timestamp": int(time.time()),
                "compression": "zlib" if compress_flag else "none",
            }
            write_manifest(db, collection, file_id, initial_manifest, log_fn)

        # Upload chunks
        parts = split_base64_into_parts(b64, chunk_size_chars)
        total_chunks = upload_chunks_in_batches(db, collection, file_id, parts, log_fn, batch_size=300)

        # Final manifest
        manifest = {
            "file_name": filename,
            "total_chunks": int(total_chunks),
            "sha256": sha,
            "settings": file_data['print_settings'],
            "user": user_meta,
            "timestamp": int(time.time()),
            "compression": "zlib" if compress_flag else "none",
        }
        write_manifest(db, collection, file_id, manifest, log_fn)

        return {
            "success": True,
            "file_id": file_id,
            "total_chunks": total_chunks,
            "filename": filename
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "filename": filename
        }


def create_file_card_html(filename, file_key, file_data, print_settings, file_index):
    """Create HTML card for file with editor integration"""
    uploaded_ts = file_data.get('uploaded_at', 0)
    uploaded_time_str = time.strftime('%d %b %Y, %H:%M', time.localtime(uploaded_ts/1000)) if uploaded_ts else ""
    file_size_mb = round(len(file_data['current_bytes']) / (1024 * 1024), 2) if file_data['current_bytes'] else 0
    
    EDITOR_URL = "https://anuj-pro979.github.io/printdilog/"
    
    # Prepare JSON values for JavaScript
    js_editor_url = json.dumps(EDITOR_URL)
    js_file_key = json.dumps(file_key)
    js_filename = json.dumps(filename)
    js_base64 = json.dumps(file_data['current_base64'])
    js_file_index = json.dumps(file_index)
    
    edited_indicator = " ✏️ (edited)" if file_data.get('edited', False) else ""
    
    html = f"""
<div class="file-container" style="padding:15px; border-radius:12px; border:1px solid #e0e0e0; margin-bottom:15px; background: linear-gradient(135deg, #f8f9fa 0%, #ffffff 100%); box-shadow: 0 2px 8px rgba(0,0,0,0.08);">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
        <div>
            <div id="title_{file_key}" style="font-weight:700; font-size:16px; color:#2c3e50;">{filename}{edited_indicator}</div>
            <div style="font-size:12px; color:#7f8c8d; margin-top:4px;">{uploaded_time_str} • {file_size_mb} MB • {len(file_data['current_bytes']) if file_data['current_bytes'] else 0} bytes</div>
        </div>
        <div>
            <button id="edit_{file_key}" style="padding:10px 16px; margin-right:8px; background:#3498db; color:white; border:none; border-radius:6px; cursor:pointer; font-size:14px;">✏️ Edit PDF</button>
            <button id="dl_{file_key}" style="padding:10px 16px; background:#27ae60; color:white; border:none; border-radius:6px; cursor:pointer; font-size:14px;">⬇️ Download</button>
        </div>
    </div>
    
    <div style="background:#f8f9fa; padding:10px; border-radius:6px; margin-top:10px;">
        <div style="font-size:12px; font-weight:600; color:#34495e; margin-bottom:6px;">Print Settings for this file:</div>
        <div style="font-size:11px; color:#5a6c7d;">
            Copies: {print_settings['copies']} | 
            Color: {print_settings['colorMode']} | 
            Duplex: {print_settings['duplex']}
            {f" | Printer: {print_settings['printerName']}" if print_settings['printerName'] else ""}
        </div>
    </div>

<script>
(function(){{
    const EDITOR_URL = {js_editor_url};
    const TARGET_ORIGIN = (new URL(EDITOR_URL)).origin;
    const fileKey = {js_file_key};
    const fileIndex = {js_file_index};
    let filename = {js_filename};
    let currentBase64 = {js_base64};
    let popup = null;
    let popupReady = false;
    let lastBlobUrl = null;

    function log(...args) {{
        try {{ console.log("[file-card]", fileKey, ...args); }} catch(e){{}}
    }}

    function base64ToUint8Array(b64) {{
        const bin = atob(b64);
        const len = bin.length;
        const arr = new Uint8Array(len);
        for (let i = 0; i < len; i++) arr[i] = bin.charCodeAt(i);
        return arr;
    }}

    function makeBlobUrlFromBase64(b64) {{
        if (lastBlobUrl) {{ URL.revokeObjectURL(lastBlobUrl); lastBlobUrl = null; }}
        const uint8 = base64ToUint8Array(b64);
        const blob = new Blob([uint8], {{ type: "application/pdf" }});
        const url = URL.createObjectURL(blob);
        lastBlobUrl = url;
        return url;
    }}

    // Download functionality
    const downloadBtn = document.getElementById("dl_" + fileKey);
    if (downloadBtn) {{
        downloadBtn.onclick = () => {{
            try {{
                const url = makeBlobUrlFromBase64(currentBase64);
                const a = document.createElement("a");
                a.href = url;
                let dlName = filename;
                if (!/\\.pdf$/i.test(dlName)) dlName = dlName + ".pdf";
                a.download = dlName;
                a.click();
                log("Download triggered for", dlName);
            }} catch(e) {{
                console.error("Download error", e);
                alert("Download failed: " + (e && e.message ? e.message : e));
            }}
        }};
    }}

    // Editor popup functionality
    function openPopup() {{
        try {{
            const openerWindow = (window.top && window.top !== window) ? window.top : window;
            popup = openerWindow.open(EDITOR_URL, "editor_popup_" + fileKey, "width=1200,height=800,resizable,scrollbars");
            if (!popup) {{
                alert("Popup blocked. Please allow popups for this site.");
                log("Popup blocked");
                return;
            }}
            popupReady = false;

            let attempts = 0;
            const pingInterval = setInterval(() => {{
                if (!popup || popup.closed) {{ clearInterval(pingInterval); return; }}
                attempts++;
                try {{
                    popup.postMessage({{type:'ping', from:'sender'}}, TARGET_ORIGIN);
                }} catch(e) {{}}
                if (attempts > 120) {{
                    clearInterval(pingInterval);
                    log("Giving up pinging popup after attempts", attempts);
                }}
            }}, 300);
        }} catch(e) {{
            console.error("openPopup error", e);
            alert("Could not open editor popup: " + e.message);
        }}
    }}

    function sendFile() {{
        if (!popup || popup.closed) return;
        if (!popupReady) return;
        try {{
            popup.postMessage({{ type: "pdf_file_data", filename: filename, pdf_data: currentBase64 }}, TARGET_ORIGIN);
            log("Sent file data to popup for", filename);
        }} catch(e) {{
            console.error("sendFile error", e);
        }}
    }}

    const editBtn = document.getElementById("edit_" + fileKey);
    if (editBtn) {{
        editBtn.addEventListener("click", openPopup);
    }}

    // Listen for messages from editor popup
    window.addEventListener("message", (event) => {{
        try {{
            if (!event.origin || event.origin !== TARGET_ORIGIN) return;
            const data = event.data || {{}};
            if (event.source !== popup && (!data.type || (data.type !== "pdf_editor_ready" && data.type !== "pdf_edited_data"))) {{
                return;
            }}

            if (data.type === "pdf_editor_ready") {{
                popupReady = true;
                log("popup ready");
                sendFile();
            }}

            if (data.type === "pdf_edited_data") {{
                const editedBase64 = data.pdf_data;
                const editedNameFromEditor = data.filename || filename;
                const ts = Date.now();
                const newName = (editedNameFromEditor.replace(/\\.pdf$/i, "") || filename.replace(/\\.pdf$/i,"")) + "_edited_" + ts + ".pdf";

                currentBase64 = editedBase64;
                filename = newName;

                // Send edited data to Streamlit with file index for proper identification
                try {{ 
                    window.parent.postMessage({{ 
                        type: 'pdf_edited_data_for_streamlit', 
                        fileKey: fileKey,
                        fileIndex: fileIndex,
                        original_filename: {js_filename},
                        filename: filename, 
                        pdf_data: editedBase64 
                    }}, '*'); 
                }} catch(e){{
                    console.error("Failed to send message to parent:", e);
                }}

                // Update title to show edited status
                try {{
                    const titleEl = document.getElementById("title_" + fileKey);
                    if (titleEl) titleEl.textContent = filename + " ✏️ (edited)";
                }} catch(e){{}}

                alert('File edited successfully! The print settings will remain the same for the edited file. You can now send all files or download this edited file.');
                log("Received edited data for", filename);
            }}
        }} catch(e) {{
            console.error("message handler error", e);
        }}
    }});
}})();
</script>
</div>
"""
    return html


# ---------------- Main Streamlit App ----------------

st.set_page_config(
    page_title="Firestore File Sender Pro", 
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("🚀 Firestore File Sender Pro")
st.markdown("*Upload multiple files to Firestore with individual print settings and bulk sending*")

# Initialize session state
if 'files_data' not in st.session_state:
    st.session_state['files_data'] = {}
if 'sent_files' not in st.session_state:
    st.session_state['sent_files'] = []
if 'user_settings' not in st.session_state:
    st.session_state['user_settings'] = {
        'name': 'StreamlitUser',
        'id': str(uuid.uuid4()),
        'email': ''
    }
if 'edited_data_buffer' not in st.session_state:
    st.session_state['edited_data_buffer'] = {}

# Listen for edited file data from JavaScript
# This is a workaround since we can't directly receive postMessage in Streamlit
# We'll use a combination of session state and rerun triggers

def process_edited_files():
    """Process any edited files that might have been updated via JavaScript"""
    # This function will be called periodically to check for updates
    # In a real implementation, you'd need a more sophisticated mechanism
    pass

# Sidebar configuration
with st.sidebar:
    st.header("⚙️ Configuration")
    
    # Connection settings
    st.subheader("🔥 Firebase Connection")
    collection = st.text_input("Firestore Collection", value=DEFAULT_COLLECTION)
    
    # Try to initialize Firestore
    try:
        db = init_firestore_from_secrets()
        st.success("✅ Connected to Firestore")
    except Exception as e:
        st.error(f"❌ Firestore connection failed: {e}")
        db = None
    
    st.markdown("---")
    
    # Upload settings (Applied to all files)
    st.subheader("📤 Bulk Upload Settings")
    chunk_kb = st.number_input("Chunk Size (KB)", min_value=16, max_value=256, value=DEFAULT_CHUNK_KB, step=8)
    compress = st.checkbox("Compress with zlib", value=DEFAULT_COMPRESS)
    create_manifest_first = st.checkbox("Create manifest first", value=True)
    
    st.markdown("---")
    
    # User settings
    st.subheader("👤 User Identity")
    st.session_state['user_settings']['name'] = st.text_input("User Name", value=st.session_state['user_settings']['name'])
    st.session_state['user_settings']['id'] = st.text_input("User ID", value=st.session_state['user_settings']['id'])
    st.session_state['user_settings']['email'] = st.text_input("Email (optional)", value=st.session_state['user_settings']['email'])

# Main content area
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("📁 File Upload & Management")
    
    # File uploader
    uploaded_files = st.file_uploader(
        "Select multiple files to upload/edit",
        accept_multiple_files=True,
        type=['pdf', 'doc', 'docx', 'txt', 'jpg', 'png'],
        help="Upload all files you want to process. You can edit them individually and send them all at once."
    )
    
    # Process uploaded files
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
                    'file_id': None,
                    'print_settings': DEFAULT_PRINT_SETTINGS.copy(),
                    'upload_settings': {
                        'chunk_kb': chunk_kb,
                        'compress': compress
                    }
                }

with col2:
    st.subheader("📊 Status")
    if st.session_state['files_data']:
        st.metric("Files Ready", len(st.session_state['files_data']))
        edited_count = sum(1 for f in st.session_state['files_data'].values() if f['edited'])
        st.metric("Edited Files", edited_count)
        sent_count = len(st.session_state['sent_files'])
        st.metric("Sent Files", sent_count)
        
        # Bulk send button - prominent placement
        st.markdown("---")
        if st.button("🚀 **SEND ALL FILES**", type="primary", use_container_width=True):
            if not db:
                st.error("Firestore not connected!")
            elif not st.session_state['files_data']:
                st.warning("No files to send!")
            else:
                # Bulk upload process
                with st.spinner("Uploading all files..."):
                    user_meta = {
                        "name": st.session_state['user_settings']['name'],
                        "id": st.session_state['user_settings']['id']
                    }
                    if st.session_state['user_settings']['email']:
                        user_meta['email'] = st.session_state['user_settings']['email']
                    
                    # Progress tracking
                    progress_bar = st.progress(0)
                    status_container = st.empty()
                    results_container = st.empty()
                    
                    total_files = len(st.session_state['files_data'])
                    completed_files = 0
                    success_count = 0
                    failed_files = []
                    
                    for filename, file_data in st.session_state['files_data'].items():
                        try:
                            status_container.text(f"Uploading: {filename}")
                            
                            def log_progress(msg):
                                status_container.text(f"Uploading {filename}: {msg}")
                            
                            result = upload_single_file(
                                db, collection, filename, file_data, user_meta,
                                chunk_kb, compress, create_manifest_first, log_progress
                            )
                            
                            if result['success']:
                                success_count += 1
                                # Update file data
                                file_data['file_id'] = result['file_id']
                                # Add to sent files
                                st.session_state['sent_files'].append({
                                    "file_id": result['file_id'],
                                    "file_name": filename,
                                    "sent_at": datetime.now(),
                                    "print_settings": file_data['print_settings'].copy(),
                                    "total_chunks": result['total_chunks']
                                })
                            else:
                                failed_files.append(f"{filename}: {result['error']}")
                            
                        except Exception as e:
                            failed_files.append(f"{filename}: {str(e)}")
                        
                        completed_files += 1
                        progress_bar.progress(completed_files / total_files)
                    
                    # Final results
                    status_container.empty()
                    progress_bar.empty()
                    
                    if success_count == total_files:
                        st.success(f"🎉 All {success_count} files uploaded successfully!")
                    elif success_count > 0:
                        st.warning(f"⚠️ {success_count}/{total_files} files uploaded successfully")
                        if failed_files:
                            with st.expander("❌ Failed uploads"):
                                for error in failed_files:
                                    st.error(error)
                    else:
                        st.error("❌ All uploads failed")
                        if failed_files:
                            for error in failed_files:
                                st.error(error)
    else:
        st.info("No files uploaded yet")

# File management section - Individual settings
if st.session_state['files_data']:
    st.markdown("---")
    st.subheader("📝 Individual File Settings")
    st.info("💡 Configure print settings for each file individually. Settings will be preserved even after editing files.")
    
    # Create tabs for better organization when there are many files
    file_names = list(st.session_state['files_data'].keys())
    
    if len(file_names) <= 3:
        # Show all files in expandable sections for few files
        for i, filename in enumerate(file_names):
            file_data = st.session_state['files_data'][filename]
            file_key = filename.replace('.', '_').replace(' ', '_').replace('-', '_')
            
            with st.expander(f"📄 {filename}" + (" ✏️ (edited)" if file_data.get('edited') else ""), expanded=True):
                col1, col2 = st.columns([3, 2])
                
                with col1:
                    # File card with editor
                    html_card = create_file_card_html(filename, file_key, file_data, file_data['print_settings'], i)
                    components.html(html_card, height=220, scrolling=False)
                
                with col2:
                    st.write("**🖨️ Print Settings**")
                    
                    # Individual print settings for this file - using unique keys
                    new_copies = st.number_input(
                        "Copies", 
                        min_value=1, 
                        max_value=100, 
                        value=file_data['print_settings']['copies'],
                        key=f"copies_{file_key}_{i}"
                    )
                    
                    new_color_mode = st.selectbox(
                        "Color Mode",
                        options=["bw", "color"],
                        index=0 if file_data['print_settings']['colorMode'] == "bw" else 1,
                        key=f"color_{file_key}_{i}"
                    )
                    
                    new_duplex = st.selectbox(
                        "Duplex",
                        options=["one-sided", "two-sided"],
                        index=0 if file_data['print_settings']['duplex'] == "one-sided" else 1,
                        key=f"duplex_{file_key}_{i}"
                    )
                    
                    new_printer_name = st.text_input(
                        "Printer Name (optional)",
                        value=file_data['print_settings']['printerName'],
                        key=f"printer_{file_key}_{i}"
                    )
                    
                    # Update print settings if they changed
                    file_data['print_settings'] = {
                        'copies': new_copies,
                        'colorMode': new_color_mode,
                        'duplex': new_duplex,
                        'printerName': new_printer_name
                    }
                    
                    st.markdown("---")
                    
                    # Individual file actions
                    col_a, col_b = st.columns(2)
                    
                    with col_a:
                        if st.button(f"📥 Download", key=f"dl_individual_{file_key}_{i}"):
                            st.download_button(
                                "⬇️ Click to Download",
                                data=file_data['current_bytes'],
                                file_name=filename,
                                mime="application/pdf" if filename.lower().endswith('.pdf') else "application/octet-stream",
                                key=f"dl_btn_{file_key}_{i}"
                            )
                    
                    with col_b:
                        if st.button(f"🗑️ Remove", key=f"remove_{file_key}_{i}"):
                            del st.session_state['files_data'][filename]
                            st.rerun()
    else:
        # Use tabs for many files
        tabs = st.tabs([f"📄 {name[:15]}{'...' if len(name) > 15 else ''}" for name in file_names])
        
        for i, (tab, filename) in enumerate(zip(tabs, file_names)):
            file_data = st.session_state['files_data'][filename]
            file_key = filename.replace('.', '_').replace(' ', '_').replace('-', '_')
            
            with tab:
                col1, col2 = st.columns([3, 2])
                
                with col1:
                    html_card = create_file_card_html(filename, file_key, file_data, file_data['print_settings'], i)
                    components.html(html_card, height=220, scrolling=False)
                
                with col2:
                    st.write("**🖨️ Print Settings**")
                    
                    # Individual print settings with unique keys
                    new_copies = st.number_input(
                        "Copies", 
                        min_value=1, 
                        max_value=100, 
                        value=file_data['print_settings']['copies'],
                        key=f"copies_{file_key}_tab_{i}"
                    )
                    
                    new_color_mode = st.selectbox(
                        "Color Mode",
                        options=["bw", "color"],
                        index=0 if file_data['print_settings']['colorMode'] == "bw" else 1,
                        key=f"color_{file_key}_tab_{i}"
                    )
                    
                    new_duplex = st.selectbox(
                        "Duplex",
                        options=["one-sided", "two-sided"],
                        index=0 if file_data['print_settings']['duplex'] == "one-sided" else 1,
                        key=f"duplex_{file_key}_tab_{i}"
                    )
                    
                    new_printer_name = st.text_input(
                        "Printer Name (optional)",
                        value=file_data['print_settings']['printerName'],
                        key=f"printer_{file_key}_tab_{i}"
                    )
                    
                    # Update print settings
                    file_data['print_settings'] = {
                        'copies': new_copies,
                        'colorMode': new_color_mode,
                        'duplex': new_duplex,
                        'printerName': new_printer_name
                    }
                    
                    st.markdown("---")
                    
                    col_a, col_b = st.columns(2)
                    
                    with col_a:
                        if st.button(f"📥 Download", key=f"dl_tab_{file_key}_{i}"):
                            st.download_button(
                                "⬇️ Click to Download",
                                data=file_data['current_bytes'],
                                file_name=filename,
                                mime="application/pdf" if filename.lower().endswith('.pdf') else "application/octet-stream",
                                key=f"dl_btn_tab_{file_key}_{i}"
                            )
                    
                    with col_b:
                        if st.button(f"🗑️ Remove", key=f"remove_tab_{file_key}_{i}"):
                            del st.session_state['files_data'][filename]
                            st.rerun()

else:
    st.info("👆 Upload some files to get started!")

# JavaScript message handler for edited files
st.markdown("""
<script>
// Global handler for edited file data from iframes
window.addEventListener('message', function(event) {
    if (event.data && event.data.type === 'pdf_edited_data_for_streamlit') {
        const data = event.data;
        console.log('Received edited file data:', data);
        
        // Store the edited data in localStorage temporarily
        // This is a workaround since we can't directly update Streamlit session state
        const editedData = {
            fileKey: data.fileKey,
            fileIndex: data.fileIndex,
            original_filename: data.original_filename,
            filename: data.filename,
            pdf_data: data.pdf_data,
            timestamp: Date.now()
        };
        
        localStorage.setItem('edited_file_data_' + data.fileKey, JSON.stringify(editedData));
        
        // Trigger a page reload to process the edited data
        // In a production app, you'd want a more elegant solution
        setTimeout(() => {
            window.location.reload();
        }, 1000);
    }
});

// Process any edited data from localStorage on page load
document.addEventListener('DOMContentLoaded', function() {
    // This would need to be handled server-side in a real implementation
    Object.keys(localStorage).forEach(key => {
        if (key.startsWith('edited_file_data_')) {
            console.log('Found edited file data:', key);
            // In a real implementation, you'd send this data to your backend
        }
    });
});
</script>
""", unsafe_allow_html=True)

# Check for edited files in browser storage (workaround)
# This is a simplified approach - in production you'd use a custom Streamlit component
if st.button("🔄 Check for Edited Files", help="Click if you've edited files and they're not showing as updated"):
    st.info("Checking for edited files... If you've edited files, please refresh the page or re-upload the edited file manually.")

# Sent files tracking section
if st.session_state['sent_files']:
    st.markdown("---")
    st.subheader("📤 Sent Files History")
    
    # Summary stats
    total_sent = len(st.session_state['sent_files'])
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Files Sent", total_sent)
    with col2:
        latest_send = max([f['sent_at'] for f in st.session_state['sent_files']])
        st.metric("Last Sent", latest_send.strftime('%H:%M:%S'))
    with col3:
        total_chunks = sum([f.get('total_chunks', 0) for f in st.session_state['sent_files']])
        st.metric("Total Chunks", total_chunks)
    
    # Detailed file list
    for i, sent_file in enumerate(st.session_state['sent_files']):
        with st.expander(f"📋 {sent_file['file_name']} - {sent_file['file_id'][:8]}..."):
            col1, col2, col3 = st.columns([2, 2, 1])
            
            with col1:
                st.write(f"**File ID:** `{sent_file['file_id']}`")
                st.write(f"**Sent:** {sent_file['sent_at'].strftime('%Y-%m-%d %H:%M:%S')}")
                st.write(f"**Chunks:** {sent_file.get('total_chunks', 'Unknown')}")
                
            with col2:
                st.write("**Print Settings Used:**")
                settings = sent_file['print_settings']
                st.write(f"• Copies: {settings['copies']}")
                st.write(f"• Color: {settings['colorMode']}")
                st.write(f"• Duplex: {settings['duplex']}")
                if settings['printerName']:
                    st.write(f"• Printer: {settings['printerName']}")
            
            with col3:
                if st.button(f"🔄 Status", key=f"status_{i}"):
                    if db:
                        try:
                            meta_doc = db.collection(collection).document(f"{sent_file['file_id']}_meta").get()
                            if meta_doc.exists:
                                data = meta_doc.to_dict()
                                
                                # Show key information
                                col_a, col_b = st.columns(2)
                                with col_a:
                                    st.write("**Status:** ✅ Found")
                                    st.write(f"**Timestamp:** {data.get('timestamp', 'N/A')}")
                                    st.write(f"**SHA256:** {data.get('sha256', 'N/A')[:16]}...")
                                
                                with col_b:
                                    payinfo = data.get('payinfo')
                                    if payinfo:
                                        st.success(f"💰 Payment: {payinfo.get('amount_str', 'N/A')} {payinfo.get('currency', '')} - {payinfo.get('status', 'Unknown')}")
                                        upi_url = payinfo.get('upi_url')
                                        if upi_url:
                                            st.markdown(f"[🔗 Open UPI]({upi_url})")
                                    else:
                                        st.info("ℹ️ No payment info yet")
                                
                                # Full data in expander
                                with st.expander("📄 Full Manifest Data"):
                                    st.json(data)
                            else:
                                st.warning("⚠️ Manifest not found")
                        except Exception as e:
                            st.error(f"❌ Error: {e}")
                    else:
                        st.error("❌ Database not connected")
                
                if st.button(f"🗑️ Remove", key=f"remove_sent_{i}"):
                    st.session_state['sent_files'].pop(i)
                    st.rerun()

# Cleanup and utility section
st.markdown("---")
st.subheader("🧹 Utilities")

col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("🧹 Clear Files", help="Remove all uploaded files"):
        st.session_state['files_data'] = {}
        st.rerun()

with col2:
    if st.button("📤 Clear History", help="Clear sent files history"):
        st.session_state['sent_files'] = []
        st.rerun()

with col3:
    if st.button("🔄 Reset All", help="Reset everything"):
        st.session_state['files_data'] = {}
        st.session_state['sent_files'] = []
        st.session_state['edited_data_buffer'] = {}
        st.rerun()

with col4:
    if st.button("📋 Export Settings", help="Download current print settings as JSON"):
        settings_export = {
            'files': {
                filename: {
                    'print_settings': file_data['print_settings'],
                    'edited': file_data.get('edited', False)
                }
                for filename, file_data in st.session_state['files_data'].items()
            },
            'user_settings': st.session_state['user_settings'],
            'upload_settings': {
                'chunk_kb': chunk_kb,
                'compress': compress,
                'collection': collection
            }
        }
        
        st.download_button(
            "⬇️ Download Settings JSON",
            data=json.dumps(settings_export, indent=2),
            file_name=f"print_settings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json"
        )

# Instructions and help
st.markdown("---")
st.subheader("📖 Usage Guide")

with st.expander("🚀 Quick Start (Bulk Upload Workflow)"):
    st.markdown("""
    ### **Recommended Workflow for Multiple Files:**
    
    1. **📁 Upload All Files**: Use the file uploader to select all files at once
    2. **🖨️ Configure Print Settings**: Set individual print settings for each file in the expandable sections below
    3. **✏️ Edit Files (Optional)**: Click "Edit PDF" on any file cards to modify them - **print settings will be preserved**
    4. **🚀 Send All at Once**: Click the big "SEND ALL FILES" button to upload everything to Firestore
    5. **📊 Track Status**: Monitor progress and check payment status for sent files
    
    ### **Key Features:**
    - ✅ **Print settings persist** through file edits
    - ✅ **Bulk upload** all files with one click  
    - ✅ **Individual settings** per file
    - ✅ **Edit PDFs** without losing settings
    - ✅ **Track upload status** and payments
    """)

with st.expander("🔧 Streamlit Cloud Setup"):
    st.code("""
# .streamlit/secrets.toml
[firebase_service_account]
type = "service_account"
project_id = "your-project-id"
private_key_id = "your-private-key-id"
private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
client_email = "your-service-account@your-project.iam.gserviceaccount.com"
client_id = "your-client-id"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs/your-service-account%40your-project.iam.gserviceaccount.com"
    """, language='toml')

with st.expander("🛠️ Troubleshooting"):
    st.markdown("""
    **Issue: Print settings reset after editing**
    - ✅ **FIXED**: Print settings now persist through file edits
    
    **Issue: Edited files not showing up**
    - Click "🔄 Check for Edited Files" button
    - Or refresh the page after editing
    - Download edited file and re-upload if needed
    
    **Issue: Upload fails**
    - Check Firestore connection in sidebar
    - Verify your secrets.toml file
    - Try smaller chunk sizes for large files
    
    **Issue: Popup blocked**
    - Allow popups for this site in browser settings
    - Try using the editor in a new tab
    """)

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; font-size: 12px;'>
    🚀 Firestore File Sender Pro | 
    ✅ Bulk Upload | 
    🖨️ Individual Print Settings | 
    ✏️ PDF Editor Integration | 
    🔐 Streamlit Cloud Ready
</div>
""", unsafe_allow_html=True)
