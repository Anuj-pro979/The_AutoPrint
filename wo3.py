# firebase_file_share.py
# Simple file sharing with Firebase - No conversions, just upload and share
#
# Run: streamlit run firebase_file_share.py

import streamlit as st
import os
import tempfile
import base64
import time
import json
import logging
import traceback
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from pathlib import Path
import hashlib
import datetime
import uuid
import io

# Firestore
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIRESTORE_AVAILABLE = True
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None
    FIRESTORE_AVAILABLE = False

# QR generation for sharing links
try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

# --------- Logging ----------
def setup_logger():
    logger = logging.getLogger("file_share")
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

logger = setup_logger()

def log_message(msg: str, level: str = "info"):
    """Logging with Streamlit integration"""
    if level == "debug":
        logger.debug(msg)
    elif level == "warning":
        logger.warning(msg)
        st.warning(f"⚠️ {msg}")
    elif level == "error":
        logger.error(msg)
        st.error(f"❌ {msg}")
    else:
        logger.info(msg)
        st.info(f"ℹ️ {msg}")

# --------- Data classes ----------
@dataclass
class SharedFile:
    file_id: str
    filename: str
    file_bytes: bytes
    file_type: str
    file_size: int
    upload_time: datetime.datetime
    uploader_name: str
    share_code: str
    downloads: int = 0

# --------- Streamlit Configuration ----------
st.set_page_config(
    page_title="Simple File Share", 
    layout="wide", 
    page_icon="📁",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main {
        padding-top: 1rem;
    }
    .file-item {
        border: 1px solid #e0e0e0;
        border-radius: 5px;
        padding: 0.5rem;
        margin: 0.5rem 0;
        background-color: #f8f9fa;
    }
    .share-code {
        background-color: #e3f2fd;
        border-left: 4px solid #2196f3;
        padding: 1rem;
        margin: 0.5rem 0;
        font-family: monospace;
        font-size: 1.2em;
        text-align: center;
    }
    .success-message {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        color: #155724;
        padding: 0.75rem 1.25rem;
        margin-bottom: 1rem;
        border-radius: 0.25rem;
    }
</style>
""", unsafe_allow_html=True)

# --------- Header ----------
st.markdown("<h1 style='text-align:center; margin-bottom:2rem;'>📁 Simple File Share</h1>", unsafe_allow_html=True)

# --------- Initialize Session State ----------
def init_session_state():
    defaults = {
        'uploaded_files': [],
        'status': "",
        'user_name': "",
        'user_id': str(uuid.uuid4())[:8],
        'current_share_codes': [],
        'downloaded_files': []
    }
    
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_session_state()

def set_status(message: str):
    """Update status with timestamp"""
    timestamp = datetime.datetime.now().strftime('%H:%M:%S')
    st.session_state.status = f"{timestamp} - {message}"

# --------- Firestore Initialization ----------
COLLECTION = "files"  # SAME AS ORIGINAL
CHUNK_SIZE = 200_000  # SAME AS ORIGINAL

db = None
FIRESTORE_OK = False
FIRESTORE_ERR = None

def init_firestore():
    global db, FIRESTORE_OK, FIRESTORE_ERR
    
    if not FIRESTORE_AVAILABLE:
        FIRESTORE_ERR = "firebase_admin package not installed"
        return
    
    try:
        if not hasattr(st, "secrets") or "firebase_service_account" not in st.secrets:
            raise RuntimeError("Add 'firebase_service_account' to Streamlit Secrets")
        
        service_account_info = st.secrets["firebase_service_account"]
        
        if isinstance(service_account_info, str):
            service_account_info = json.loads(service_account_info)
        
        if "private_key" in service_account_info:
            service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")
        
        try:
            app = firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(service_account_info)
            app = firebase_admin.initialize_app(cred)
        
        db = firestore.client()
        FIRESTORE_OK = True
        set_status("Firebase initialized successfully")
        
    except Exception as e:
        FIRESTORE_OK = False
        FIRESTORE_ERR = str(e)
        set_status(f"Firebase initialization failed: {e}")
        logger.error(f"Firebase init error: {e}")

init_firestore()

# --------- Utility Functions ----------
def generate_share_code(length=6):
    """Generate a random share code"""
    import random
    import string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def format_file_size(size_bytes):
    """Convert bytes to human readable format"""
    if size_bytes == 0:
        return "0 B"
    size_names = ["B", "KB", "MB", "GB"]
    import math
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_names[i]}"

def meta_doc_id(file_id: str) -> str:
    """SAME AS ORIGINAL"""
    return f"{file_id}_meta"

def chunk_doc_id(file_id: str, chunk_index: int) -> str:
    """SAME AS ORIGINAL"""
    return f"{file_id}_{chunk_index}"

# --------- File Upload Functions ----------
def upload_file_to_firebase(uploaded_file, uploader_name: str) -> Optional[str]:
    """Upload file to Firebase and return share code - SAME STRUCTURE AS ORIGINAL"""
    
    if not FIRESTORE_OK or not db:
        st.error("❌ Firebase not available. Cannot upload files.")
        return None
    
    try:
        # Generate unique identifiers - SAME AS ORIGINAL
        file_id = str(uuid.uuid4())[:8]  # Same format as original
        share_code = generate_share_code()
        
        # Get file data
        file_bytes = uploaded_file.getvalue()
        filename = uploaded_file.name
        file_type = uploaded_file.type or "application/octet-stream"
        file_size = len(file_bytes)
        
        # Encode file data - SAME AS ORIGINAL
        b64_data = base64.b64encode(file_bytes).decode('utf-8')
        chunks = [b64_data[i:i+CHUNK_SIZE] for i in range(0, len(b64_data), CHUNK_SIZE)]
        
        set_status(f"Uploading {filename}...")
        
        # Upload chunks - SAME STRUCTURE AS ORIGINAL
        progress_bar = st.progress(0.0)
        for i, chunk in enumerate(chunks):
            chunk_doc_id_str = chunk_doc_id(file_id, i)  # Same naming as original
            
            def upload_chunk():
                doc_ref = db.collection(COLLECTION).document(chunk_doc_id_str)
                doc_ref.set({
                    "data": chunk,
                    "chunk_index": i,
                    "file_id": file_id,
                    "timestamp": datetime.datetime.now()
                })
            
            retry_with_backoff(upload_chunk, attempts=3)
            
            progress = (i + 1) / len(chunks)
            progress_bar.progress(progress)
        
        # Upload metadata - SAME STRUCTURE AS ORIGINAL
        meta_doc = {
            "total_chunks": len(chunks),
            "file_name": filename,
            "orig_filename": filename,  # Same as original
            "sha256": sha256_bytes(file_bytes),
            "file_size_bytes": file_size,
            "file_type": file_type,
            "share_code": share_code,
            "user_name": uploader_name,
            "user_id": st.session_state.user_id,
            "timestamp": datetime.datetime.now(),
            "status": "uploaded",
            "downloads": 0
        }
        
        def upload_metadata():
            meta_doc_ref = db.collection(COLLECTION).document(meta_doc_id(file_id))
            meta_doc_ref.set(meta_doc, merge=True)
        
        retry_with_backoff(upload_metadata, attempts=3)
        
        progress_bar.progress(1.0)
        set_status(f"✅ Successfully uploaded {filename}")
        
        return share_code
        
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        st.error(f"❌ Upload failed: {str(e)}")
        return None

# --------- File Download Functions ----------
def download_file_by_share_code(share_code: str) -> Optional[SharedFile]:
    """Download file using share code - SAME STRUCTURE AS ORIGINAL"""
    
    if not FIRESTORE_OK or not db:
        st.error("❌ Firebase not available. Cannot download files.")
        return None
    
    try:
        set_status(f"Looking for file with share code: {share_code}")
        
        # Find file by share code - SAME AS ORIGINAL
        files_ref = db.collection(COLLECTION)
        query = files_ref.where("share_code", "==", share_code.upper())
        docs = query.stream()
        
        file_doc = None
        for doc in docs:
            if doc.id.endswith("_meta"):  # Same pattern as original
                file_doc = doc
                break
        
        if not file_doc:
            st.error(f"❌ No file found with share code: {share_code}")
            return None
        
        file_data = file_doc.to_dict()
        file_id = file_doc.id.replace("_meta", "")  # Extract file_id same as original
        
        set_status(f"Found file: {file_data['file_name']}")
        
        # Download chunks - SAME STRUCTURE AS ORIGINAL
        chunks_data = []
        progress_bar = st.progress(0.0)
        total_chunks = file_data["total_chunks"]
        
        for chunk_index in range(total_chunks):
            chunk_doc_id_str = chunk_doc_id(file_id, chunk_index)
            
            def get_chunk():
                doc_ref = db.collection(COLLECTION).document(chunk_doc_id_str)
                doc_snapshot = doc_ref.get()
                if doc_snapshot.exists:
                    return doc_snapshot.to_dict()["data"]
                return None
            
            chunk_data = retry_with_backoff(get_chunk, attempts=3)
            if chunk_data is None:
                st.error(f"❌ Failed to download chunk {chunk_index}")
                return None
            
            chunks_data.append(chunk_data)
            progress = (chunk_index + 1) / total_chunks
            progress_bar.progress(progress)
        
        if len(chunks_data) != total_chunks:
            st.error(f"❌ File incomplete. Expected {total_chunks} chunks, got {len(chunks_data)}")
            return None
        
        # Reconstruct file - SAME AS ORIGINAL
        b64_data = ''.join(chunks_data)
        file_bytes = base64.b64decode(b64_data)
        
        # Verify integrity - SAME AS ORIGINAL
        if sha256_bytes(file_bytes) != file_data.get("sha256", ""):
            st.warning("⚠️ File integrity check failed")
        
        # Update download count - SAME AS ORIGINAL
        file_doc.reference.update({"downloads": file_data.get("downloads", 0) + 1})
        
        progress_bar.progress(1.0)
        set_status(f"✅ Successfully downloaded {file_data['file_name']}")
        
        return SharedFile(
            file_id=file_id,
            filename=file_data["file_name"],
            file_bytes=file_bytes,
            file_type=file_data.get("file_type", "application/octet-stream"),
            file_size=file_data["file_size_bytes"],
            upload_time=file_data["timestamp"],
            uploader_name=file_data["user_name"],
            share_code=share_code,
            downloads=file_data.get("downloads", 0) + 1
        )
        
    except Exception as e:
        logger.error(f"Download failed: {e}")
        st.error(f"❌ Download failed: {str(e)}")
        return None

def list_user_files(user_id: str) -> List[Dict]:
    """List files uploaded by a specific user"""
    
    if not FIRESTORE_OK or not db:
        return []
    
    try:
        files_ref = db.collection(COLLECTION)
        query = files_ref.where("uploader_id", "==", user_id).order_by("upload_time", direction=firestore.Query.DESCENDING)
        docs = query.stream()
        
        files = []
        for doc in docs:
            file_data = doc.to_dict()
            files.append(file_data)
        
        return files
        
    except Exception as e:
        logger.error(f"Failed to list user files: {e}")
        return []

# --------- Main UI ----------

# Sidebar
with st.sidebar:
    st.title("📋 File Share Info")
    
    # User info
    st.markdown("### 👤 User")
    st.write(f"**ID:** {st.session_state.user_id}")
    
    # Firebase status
    st.markdown("### 🔧 Status")
    if FIRESTORE_OK:
        st.success("✅ Firebase Connected")
    else:
        st.error("❌ Firebase Not Available")
        if FIRESTORE_ERR:
            st.error(f"Error: {FIRESTORE_ERR}")
    
    # Statistics
    if FIRESTORE_OK:
        user_files = list_user_files(st.session_state.user_id)
        st.markdown("### 📊 Your Files")
        st.write(f"**Uploaded:** {len(user_files)}")
        
        total_downloads = sum(f.get("downloads", 0) for f in user_files)
        st.write(f"**Total Downloads:** {total_downloads}")

# Main content tabs
tab1, tab2, tab3 = st.tabs(["📤 Upload Files", "📥 Download Files", "📋 My Files"])

# Upload Tab
with tab1:
    st.markdown("### 📤 Upload and Share Files")
    
    # User name input
    user_name = st.text_input(
        "Your Name (Optional)", 
        value=st.session_state.get("user_name", ""),
        placeholder="Enter your name"
    )
    st.session_state.user_name = user_name or "Anonymous"
    
    if not FIRESTORE_OK:
        st.error(f"❌ Firebase not available: {FIRESTORE_ERR}")
        st.info("Please configure Firebase credentials in Streamlit secrets to enable file sharing.")
    else:
        # File uploader
        uploaded_files = st.file_uploader(
            "Choose files to share",
            accept_multiple_files=True,
            help="Select one or more files to upload and share"
        )
        
        if uploaded_files:
            st.markdown("#### 📋 Files to Upload")
            
            total_size = sum(len(f.getvalue()) for f in uploaded_files)
            st.info(f"📊 **Total files:** {len(uploaded_files)} | **Total size:** {format_file_size(total_size)}")
            
            # Show file list
            for i, file in enumerate(uploaded_files):
                with st.container():
                    col1, col2, col3 = st.columns([3, 1, 1])
                    
                    with col1:
                        st.write(f"**{file.name}**")
                    with col2:
                        st.write(f"{format_file_size(len(file.getvalue()))}")
                    with col3:
                        st.write(f"{file.type or 'Unknown'}")
            
            # Upload button
            if st.button("🚀 Upload and Generate Share Codes", type="primary", use_container_width=True):
                share_codes = []
                
                for uploaded_file in uploaded_files:
                    with st.spinner(f"Uploading {uploaded_file.name}..."):
                        share_code = upload_file_to_firebase(uploaded_file, st.session_state.user_name)
                        if share_code:
                            share_codes.append({
                                "filename": uploaded_file.name,
                                "share_code": share_code,
                                "size": format_file_size(len(uploaded_file.getvalue()))
                            })
                
                if share_codes:
                    st.session_state.current_share_codes = share_codes
                    st.success(f"✅ Successfully uploaded {len(share_codes)} files!")
                    st.balloons()
        
        # Show generated share codes
        if st.session_state.current_share_codes:
            st.markdown("---")
            st.markdown("### 🔗 Generated Share Codes")
            
            for file_info in st.session_state.current_share_codes:
                with st.container():
                    st.markdown(f"#### 📄 {file_info['filename']}")
                    
                    col1, col2 = st.columns([2, 1])
                    
                    with col1:
                        st.markdown(f'<div class="share-code">{file_info["share_code"]}</div>', unsafe_allow_html=True)
                        st.write(f"**Size:** {file_info['size']}")
                    
                    with col2:
                        # Generate QR code if available
                        if QR_AVAILABLE:
                            try:
                                share_url = f"Share Code: {file_info['share_code']}"
                                qr = qrcode.QRCode(version=1, box_size=5, border=2)
                                qr.add_data(share_url)
                                qr.make(fit=True)
                                
                                qr_img = qr.make_image(fill_color="black", back_color="white")
                                
                                img_buffer = io.BytesIO()
                                qr_img.save(img_buffer, format='PNG')
                                img_buffer.seek(0)
                                
                                st.image(img_buffer, width=150, caption="Share Code QR")
                                
                            except Exception as e:
                                logger.warning(f"QR code generation failed: {e}")
                                st.write("QR code not available")
                    
                    st.markdown("---")

# Download Tab
with tab2:
    st.markdown("### 📥 Download Files with Share Code")
    
    if not FIRESTORE_OK:
        st.error(f"❌ Firebase not available: {FIRESTORE_ERR}")
    else:
        col1, col2 = st.columns([2, 1])
        
        with col1:
            share_code_input = st.text_input(
                "Enter Share Code",
                placeholder="e.g., ABC123",
                help="Enter the 6-character share code"
            ).upper()
        
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            download_button = st.button("📥 Download File", type="primary", use_container_width=True)
        
        if download_button and share_code_input:
            shared_file = download_file_by_share_code(share_code_input)
            
            if shared_file:
                st.success(f"✅ Found file: **{shared_file.filename}**")
                
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.write(f"**Filename:** {shared_file.filename}")
                    st.write(f"**Size:** {format_file_size(shared_file.file_size)}")
                    st.write(f"**Type:** {shared_file.file_type}")
                    st.write(f"**Uploaded by:** {shared_file.uploader_name}")
                    st.write(f"**Upload time:** {shared_file.upload_time}")
                    st.write(f"**Downloads:** {shared_file.downloads}")
                
                with col2:
                    st.download_button(
                        label="💾 Download File",
                        data=shared_file.file_bytes,
                        file_name=shared_file.filename,
                        mime=shared_file.file_type,
                        use_container_width=True
                    )
                
                # Add to downloaded files list
                if shared_file not in st.session_state.downloaded_files:
                    st.session_state.downloaded_files.append(shared_file)

# My Files Tab
with tab3:
    st.markdown("### 📋 My Uploaded Files")
    
    if not FIRESTORE_OK:
        st.error(f"❌ Firebase not available: {FIRESTORE_ERR}")
    else:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
        
        user_files = list_user_files(st.session_state.user_id)
        
        if not user_files:
            st.info("📭 You haven't uploaded any files yet.")
        else:
            st.info(f"📊 You have uploaded **{len(user_files)}** files")
            
            for file_info in user_files:
                with st.expander(f"📄 {file_info['filename']} - Code: {file_info['share_code']}"):
                    col1, col2 = st.columns([2, 1])
                    
                    with col1:
                        st.write(f"**Share Code:** `{file_info['share_code']}`")
                        st.write(f"**Size:** {format_file_size(file_info['file_size'])}")
                        st.write(f"**Type:** {file_info['file_type']}")
                        st.write(f"**Uploaded:** {file_info['upload_time']}")
                        st.write(f"**Downloads:** {file_info.get('downloads', 0)}")
                    
                    with col2:
                        st.markdown(f'<div class="share-code">{file_info["share_code"]}</div>', unsafe_allow_html=True)

# Status Display
if st.session_state.get("status"):
    st.info(f"📊 **Status:** {st.session_state.status}")

# Footer with instructions
st.markdown("---")
st.markdown("### 📖 How to Use")

col1, col2, col3 = st.columns(3)

with col1:
    st.markdown("""
    #### 📤 Upload Files
    1. Enter your name (optional)
    2. Select files to upload
    3. Click "Upload and Generate Share Codes"
    4. Share the generated codes with others
    """)

with col2:
    st.markdown("""
    #### 📥 Download Files
    1. Get a share code from someone
    2. Enter the code in the download tab
    3. Click "Download File"
    4. Save the file to your device
    """)

with col3:
    st.markdown("""
    #### 📋 Manage Files
    1. View all your uploaded files
    2. See download statistics
    3. Copy share codes to share again
    4. Monitor file usage
    """)

# Setup Instructions
with st.expander("⚙️ **Setup Instructions for Developers**"):
    st.markdown("""
    ### Firebase Setup
    
    1. **Create Firebase Project:**
       - Go to [Firebase Console](https://console.firebase.google.com/)
       - Create a new project
       - Enable Firestore Database
    
    2. **Generate Service Account:**
       - Go to Project Settings > Service Accounts
       - Generate new private key
       - Download the JSON file
    
    3. **Configure Streamlit Secrets:**
       Add to `.streamlit/secrets.toml`:
       ```toml
       [firebase_service_account]
       type = "service_account"
       project_id = "your-project-id"
       private_key_id = "your-private-key-id"
       private_key = "-----BEGIN PRIVATE KEY-----\nYOUR-PRIVATE-KEY\n-----END PRIVATE KEY-----\n"
       client_email = "your-service-account-email"
       client_id = "your-client-id"
       auth_uri = "https://accounts.google.com/o/oauth2/auth"
       token_uri = "https://oauth2.googleapis.com/token"
       auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
       client_x509_cert_url = "your-cert-url"
       ```
    
    4. **Install Dependencies:**
       ```bash
       pip install streamlit firebase-admin qrcode[pil]
       ```
    
    5. **Run the App:**
       ```bash
       streamlit run firebase_file_share.py
       ```
    
    ### Security Notes
    - Files are stored as base64 chunks in Firestore
    - Share codes are randomly generated 6-character strings
    - No authentication required (public sharing)
    - Files persist until manually deleted
    """)

st.markdown(
    "<div style='text-align: center; color: #666; padding: 1rem;'>"
    "📁 <strong>Simple File Share</strong> - No conversions, just pure file sharing<br>"
    "<small>Built with Streamlit and Firebase</small>"
    "</div>", 
    unsafe_allow_html=True
)
