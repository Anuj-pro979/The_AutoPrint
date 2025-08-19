import streamlit as st
import base64
import json
import time
import zlib
import hashlib
import uuid
from datetime import datetime
import streamlit.components.v1 as components

# Firebase imports
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False

st.set_page_config(
    page_title="PDF Editor & Print Service",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Enhanced CSS with mobile-first responsive design + print service styling
st.markdown(
    """
    <style>
    /* Global mobile-first styles */
    .main .block-container {
        padding-top: 2rem;
        padding-left: 1rem;
        padding-right: 1rem;
        max-width: 100%;
    }
    
    /* File container with modern card design */
    .file-container {
        background: linear-gradient(135deg, rgba(255,255,255,0.1) 0%, rgba(255,255,255,0.05) 100%);
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: 16px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        backdrop-filter: blur(10px);
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    
    .file-container:hover {
        transform: translateY(-4px);
        box-shadow: 0 16px 48px rgba(0,0,0,0.15);
        border-color: rgba(255,255,255,0.2);
    }
    
    /* File header with icon and name */
    .file-header {
        display: flex;
        align-items: flex-start;
        gap: 1rem;
        margin-bottom: 1rem;
    }
    
    .file-icon {
        width: 48px;
        height: 48px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 12px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.5rem;
        flex-shrink: 0;
    }
    
    .file-info {
        flex: 1;
        min-width: 0;
    }
    
    .filename {
        font-size: 1.1rem;
        font-weight: 700;
        color: #ffffff;
        margin: 0 0 0.25rem 0;
        word-break: break-word;
        line-height: 1.3;
    }
    
    .file-meta {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
        color: #8e8e93;
        font-size: 0.85rem;
    }
    
    .meta-item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    /* Action buttons container */
    .actions-container {
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
        margin-top: 1rem;
    }
    
    /* Enhanced button styles */
    .action-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
        padding: 0.875rem 1.25rem;
        border: none;
        border-radius: 12px;
        font-weight: 600;
        font-size: 0.95rem;
        cursor: pointer;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        text-decoration: none;
        min-height: 44px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }
    
    .btn-primary {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
    }
    
    .btn-secondary {
        background: rgba(255,255,255,0.1);
        color: #ffffff;
        border: 1px solid rgba(255,255,255,0.2);
    }
    
    .btn-print {
        background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        color: white;
    }
    
    .btn-primary:hover, .btn-print:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(102, 126, 234, 0.4);
    }
    
    .btn-secondary:hover {
        background: rgba(255,255,255,0.2);
        transform: translateY(-1px);
    }
    
    /* Status indicators */
    .status-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
        padding: 0.25rem 0.5rem;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    
    .status-uploaded {
        background: rgba(52, 199, 89, 0.2);
        color: #34c759;
    }
    
    .status-edited {
        background: rgba(255, 159, 10, 0.2);
        color: #ff9f0a;
    }
    
    .status-printing {
        background: rgba(0, 122, 255, 0.2);
        color: #007aff;
    }
    
    /* Print settings panel */
    .print-settings {
        background: rgba(255,255,255,0.05);
        border-radius: 12px;
        padding: 1rem;
        margin: 1rem 0;
        border: 1px solid rgba(255,255,255,0.1);
    }
    
    /* Responsive design */
    @media (min-width: 640px) {
        .actions-container {
            flex-direction: row;
            margin-top: 1.5rem;
        }
        
        .action-btn {
            flex: 1;
        }
        
        .file-meta {
            flex-direction: row;
            gap: 1rem;
        }
    }
    
    @media (min-width: 1024px) {
        .main .block-container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .actions-container {
            max-width: 500px;
            margin-left: auto;
        }
    }
    
    /* Theme adjustments */
    @media (prefers-color-scheme: light) {
        .file-container {
            background: linear-gradient(135deg, rgba(0,0,0,0.05) 0%, rgba(0,0,0,0.02) 100%);
            border-color: rgba(0,0,0,0.1);
        }
        
        .filename {
            color: #1a1a1a;
        }
        
        .file-meta {
            color: #666666;
        }
        
        .btn-secondary {
            background: rgba(0,0,0,0.05);
            color: #1a1a1a;
            border-color: rgba(0,0,0,0.1);
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Helper functions for Firestore
def retry_with_backoff(fn, max_attempts=3, initial_delay=1.0, factor=2.0):
    """Simplified retry logic"""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            attempt += 1
            if attempt >= max_attempts:
                raise
            delay = initial_delay * (factor ** (attempt - 1))
            time.sleep(delay)

def init_firestore_from_uploaded_file(uploaded_file):
    """Initialize firebase_admin from uploaded service account JSON"""
    if not uploaded_file:
        return None
    
    try:
        raw = uploaded_file.read()
        sa_dict = json.loads(raw.decode('utf-8'))
        
        # Normalize private key
        if 'private_key' in sa_dict and isinstance(sa_dict['private_key'], str):
            sa_dict['private_key'] = sa_dict['private_key'].replace('\\n', '\n').replace('\\r\\n', '\n')
        
        # Initialize Firebase if not already done
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(sa_dict)
            firebase_admin.initialize_app(cred)
        
        return firestore.client()
    except Exception as e:
        st.error(f"Failed to initialize Firestore: {e}")
        return None

def send_to_print_service(db, file_data, print_settings, user_info):
    """Send file to print service via Firestore"""
    try:
        file_id = uuid.uuid4().hex
        raw_bytes = file_data['current_bytes']
        
        # Compress and encode
        compressed = zlib.compress(raw_bytes) if print_settings.get('compress', True) else raw_bytes
        b64_data = base64.b64encode(compressed).decode('ascii')
        
        # Create manifest
        manifest = {
            "file_name": file_data['filename'],
            "total_chunks": 1,  # Simplified - single chunk for small files
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "settings": {
                "copies": print_settings.get('copies', 1),
                "colorMode": print_settings.get('color_mode', 'bw'),
                "duplex": print_settings.get('duplex', 'one-sided'),
                "printerName": print_settings.get('printer_name', ''),
            },
            "user": user_info,
            "timestamp": int(time.time()),
            "compression": "zlib" if print_settings.get('compress', True) else "none",
        }
        
        collection = print_settings.get('collection', 'files')
        
        # Write data chunk
        chunk_doc_ref = db.collection(collection).document(f"{file_id}_0")
        chunk_doc_ref.set({"chunk_index": 0, "data": b64_data})
        
        # Write manifest
        meta_doc_ref = db.collection(collection).document(f"{file_id}_meta")
        meta_doc_ref.set(manifest)
        
        return file_id, True
    except Exception as e:
        st.error(f"Print service error: {e}")
        return None, False

# Header
st.markdown("""
    <div style="text-align: center; margin-bottom: 2rem;">
        <h1 style="font-size: 2.5rem; font-weight: 800; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 0.5rem;">
            📄 PDF Editor & Print Service
        </h1>
        <p style="font-size: 1.1rem; color: #8e8e93; margin: 0;">
            Edit PDFs and send them to print with ease
        </p>
    </div>
""", unsafe_allow_html=True)

# Sidebar configuration
with st.sidebar:
    st.header("🔧 Configuration")
    
    # Firebase configuration
    st.subheader("Print Service Setup")
    if FIREBASE_AVAILABLE:
        sa_upload = st.file_uploader(
            "Firebase Service Account JSON", 
            type=["json"],
            help="Upload your Firebase service account JSON file"
        )
        collection = st.text_input("Firestore Collection", value="files")
        
        # Initialize Firestore
        db = init_firestore_from_uploaded_file(sa_upload) if sa_upload else None
        
        if db:
            st.success("✅ Print service connected")
        else:
            st.info("Upload service account to enable printing")
    else:
        st.error("Firebase Admin SDK not installed. Run: pip install firebase-admin")
        db = None
    
    st.markdown("---")
    
    # User information
    st.subheader("👤 User Information")
    user_name = st.text_input("Name", value="User")
    user_email = st.text_input("Email", placeholder="user@example.com")
    
    st.markdown("---")
    
    # Print settings
    st.subheader("🖨️ Print Settings")
    copies = st.number_input("Copies", min_value=1, max_value=10, value=1)
    color_mode = st.selectbox("Color Mode", ["bw", "color"], index=0)
    duplex = st.selectbox("Duplex", ["one-sided", "two-sided"], index=0)
    printer_name = st.text_input("Printer Name", placeholder="Optional")
    compress = st.checkbox("Compress Files", value=True)

# Initialize session state
if 'files_data' not in st.session_state:
    st.session_state.files_data = {}
if 'print_jobs' not in st.session_state:
    st.session_state.print_jobs = []

# File upload
uploaded_files = st.file_uploader(
    "Choose PDF files", 
    type=["pdf"], 
    accept_multiple_files=True,
    help="Select PDF files to edit or print"
)

# Process uploaded files
if uploaded_files:
    for uploaded_file in uploaded_files:
        if uploaded_file.name not in st.session_state.files_data:
            file_bytes = uploaded_file.read()
            base64_pdf = base64.b64encode(file_bytes).decode("utf-8")
            st.session_state.files_data[uploaded_file.name] = {
                'original_base64': base64_pdf,
                'current_base64': base64_pdf,
                'original_bytes': file_bytes,
                'current_bytes': file_bytes,
                'edited': False,
                'uploaded_at': int(time.time() * 1000),
                'filename': uploaded_file.name
            }

# Display files
if not st.session_state.get('files_data'):
    st.markdown("""
        <div style="border: 2px dashed rgba(255,255,255,0.3); border-radius: 16px; padding: 2rem; text-align: center; margin: 2rem 0; background: rgba(255,255,255,0.05);">
            <div style="font-size: 3rem; margin-bottom: 1rem;">📎</div>
            <h3 style="margin: 0 0 0.5rem 0; color: #ffffff;">No files uploaded yet</h3>
            <p style="margin: 0; color: #8e8e93;">Choose PDF files above to get started</p>
        </div>
    """, unsafe_allow_html=True)
else:
    st.markdown(f"""
        <div style="background: linear-gradient(135deg, rgba(52, 199, 89, 0.1) 0%, rgba(52, 199, 89, 0.05) 100%); border: 1px solid rgba(52, 199, 89, 0.3); border-radius: 12px; padding: 1rem; margin: 1rem 0; color: #34c759; font-weight: 600;">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
                <span style="font-size: 1.2rem;">✅</span>
                <span>{len(st.session_state['files_data'])} file(s) ready</span>
            </div>
        </div>
    """, unsafe_allow_html=True)

    # Render file cards
    for filename, fd in st.session_state['files_data'].items():
        file_key = filename.replace('.', '_').replace(' ', '_').replace('-', '_')
        EDITOR_URL = "https://anuj-pro979.github.io/printdilog/"
        TARGET_ORIGIN = "https://anuj-pro979.github.io"

        uploaded_ts = fd.get('uploaded_at', 0)
        uploaded_time_str = time.strftime('%d %b %Y, %H:%M', time.localtime(uploaded_ts/1000)) if uploaded_ts else ""
        file_size_mb = round(len(fd['current_bytes']) / (1024 * 1024), 2) if fd['current_bytes'] else 0
        
        status_badge = "status-edited" if fd.get('edited') else "status-uploaded"
        status_text = "Edited" if fd.get('edited') else "Ready"
        status_icon = "✏️" if fd.get('edited') else "📤"

        # File card with integrated print functionality
        col1, col2 = st.columns([3, 1])
        
        with col1:
            st.markdown(f"""
                <div class="file-container">
                    <div class="file-header">
                        <div class="file-icon">📄</div>
                        <div class="file-info">
                            <h3 class="filename">{filename}</h3>
                            <div class="status-badge {status_badge}">
                                <span>{status_icon}</span>
                                <span>{status_text}</span>
                            </div>
                        </div>
                    </div>
                    
                    <div class="file-meta">
                        <div class="meta-item">
                            <span>📅</span>
                            <span>{uploaded_time_str}</span>
                        </div>
                        <div class="meta-item">
                            <span>📊</span>
                            <span>{file_size_mb} MB</span>
                        </div>
                    </div>
                </div>
            """, unsafe_allow_html=True)
        
        with col2:
            # Action buttons
            if st.button(f"✏️ Edit", key=f"edit_{file_key}", use_container_width=True):
                # JavaScript for editor popup (simplified)
                components.html(f"""
                    <script>
                    window.open("{EDITOR_URL}", "editor", "width=1200,height=800");
                    </script>
                """, height=50)
            
            if st.button(f"⬇️ Download", key=f"dl_{file_key}", use_container_width=True):
                # Create download
                st.download_button(
                    "📥 Download PDF",
                    data=fd['current_bytes'],
                    file_name=filename,
                    mime="application/pdf",
                    key=f"download_{file_key}"
                )
            
            # Print button
            if db and st.button(f"🖨️ Print", key=f"print_{file_key}", use_container_width=True):
                with st.spinner("Sending to print service..."):
                    print_settings = {
                        'copies': copies,
                        'color_mode': color_mode,
                        'duplex': duplex,
                        'printer_name': printer_name,
                        'compress': compress,
                        'collection': collection
                    }
                    
                    user_info = {
                        'name': user_name,
                        'id': str(uuid.uuid4()),
                        'email': user_email if user_email else None
                    }
                    
                    job_id, success = send_to_print_service(db, fd, print_settings, user_info)
                    
                    if success:
                        st.success(f"✅ Print job submitted! ID: {job_id[:8]}")
                        st.session_state.print_jobs.append({
                            'job_id': job_id,
                            'filename': filename,
                            'timestamp': time.time(),
                            'status': 'submitted'
                        })
                    else:
                        st.error("❌ Print job failed")

# Print jobs status
if st.session_state.print_jobs:
    st.markdown("---")
    st.subheader("🖨️ Print Jobs Status")
    
    for job in st.session_state.print_jobs[-5:]:  # Show last 5 jobs
        with st.expander(f"Job: {job['filename']} ({job['job_id'][:8]})"):
            col1, col2 = st.columns(2)
            
            with col1:
                st.write(f"**File:** {job['filename']}")
                st.write(f"**Job ID:** {job['job_id']}")
                st.write(f"**Status:** {job['status']}")
            
            with col2:
                if st.button(f"Check Status", key=f"status_{job['job_id'][:8]}"):
                    if db:
                        try:
                            doc = db.collection(collection).document(f"{job['job_id']}_meta").get()
                            if doc.exists:
                                data = doc.to_dict()
                                st.json(data)
                                
                                # Check for payment info
                                payinfo = data.get('payinfo')
                                if payinfo:
                                    st.success(f"💰 Payment: {payinfo.get('amount_str', 'N/A')} {payinfo.get('currency', '')}")
                                    upi_url = payinfo.get('upi_url')
                                    if upi_url:
                                        st.link_button("💳 Pay via UPI", upi_url)
                            else:
                                st.warning("Job not found")
                        except Exception as e:
                            st.error(f"Error checking status: {e}")

# Clear all files button
if st.session_state.files_data:
    if st.button("🗑️ Clear All Files", use_container_width=True):
        st.session_state.files_data = {}
        st.experimental_rerun()

# Footer
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #8e8e93; font-size: 0.9rem;'>"
    "💡 Upload PDFs to edit them online or send them directly to print service"
    "</div>", 
    unsafe_allow_html=True
)
