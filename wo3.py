# streamlit_pdf_uploader.py
# Streamlit PDF-only uploader with Firestore integration
#
# Run: streamlit run streamlit_pdf_uploader.py

import streamlit as st
import os
import base64
import time
import json
import logging
import datetime
import uuid
import webbrowser
import io
import hashlib
from typing import Optional, List, Dict
from dataclasses import dataclass
from PIL import Image
import platform

# PDF processing
try:
    from pypdf import PdfReader
    PDF_READER_AVAILABLE = True
except ImportError:
    try:
        from PyPDF2 import PdfReader
        PDF_READER_AVAILABLE = True
    except ImportError:
        PdfReader = None
        PDF_READER_AVAILABLE = False

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

# QR generation
try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

# --------- Logging ----------
def setup_logger():
    logger = logging.getLogger("pdf_uploader")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

logger = setup_logger()

# --------- Data Classes ----------
@dataclass
class PDFFile:
    filename: str
    pdf_bytes: bytes
    pages: int
    size_mb: float
    file_id: str = ""

# --------- Utilities ----------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def count_pdf_pages(pdf_bytes: bytes) -> int:
    """Count pages in PDF"""
    if not PDF_READER_AVAILABLE or not pdf_bytes:
        return 1
    
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception as e:
        logger.warning(f"Failed to count PDF pages: {e}")
        # Estimate based on file size
        size_kb = len(pdf_bytes) / 1024
        return max(1, int(size_kb / 50))

def generate_upi_uri(upi_id: str, amount: float, note: str = None) -> str:
    params = [f"pa={upi_id}", f"am={amount:.2f}"]
    if note:
        params.append(f"tn={note}")
    return "upi://pay?" + "&".join(params)

# --------- Streamlit Configuration ----------
st.set_page_config(
    page_title="PDF Upload Service", 
    layout="wide", 
    page_icon="📄",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main { padding-top: 1rem; }
    .stButton > button { width: 100%; }
    .pdf-item {
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 1rem;
        margin: 0.5rem 0;
        background-color: #f8f9fa;
    }
    .success-box {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        color: #155724;
        padding: 1rem;
        border-radius: 8px;
        margin: 1rem 0;
    }
    .info-box {
        background-color: #e3f2fd;
        border-left: 4px solid #2196f3;
        padding: 1rem;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

# --------- Header ----------
st.markdown("<h1 style='text-align:center; margin-bottom:2rem;'>📄 PDF Upload Service</h1>", unsafe_allow_html=True)

# --------- Session State ----------
if 'pdf_files' not in st.session_state:
    st.session_state.pdf_files = []
if 'user_id' not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())[:8]
if 'upload_status' not in st.session_state:
    st.session_state.upload_status = ""
if 'payment_info' not in st.session_state:
    st.session_state.payment_info = None

# --------- Firestore Setup ----------
db = None
FIRESTORE_OK = False

def init_firestore():
    global db, FIRESTORE_OK
    
    if not FIRESTORE_AVAILABLE:
        return False
    
    try:
        if not hasattr(st, "secrets") or "firebase_service_account" not in st.secrets:
            st.error("❌ Firebase credentials not found in Streamlit Secrets")
            return False
        
        service_account_info = st.secrets["firebase_service_account"]
        
        if isinstance(service_account_info, str):
            service_account_info = json.loads(service_account_info)
        
        if "private_key" in service_account_info:
            service_account_info["private_key"] = service_account_info["private_key"].replace("\\n", "\n")
        
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
        
        db = firestore.client()
        FIRESTORE_OK = True
        return True
        
    except Exception as e:
        st.error(f"❌ Firestore initialization failed: {e}")
        return False

FIRESTORE_OK = init_firestore()

# --------- File Upload Functions ----------
def process_pdf_files(uploaded_files):
    """Process uploaded PDF files"""
    pdf_files = []
    
    for uploaded_file in uploaded_files:
        try:
            pdf_bytes = uploaded_file.getvalue()
            pages = count_pdf_pages(pdf_bytes)
            size_mb = len(pdf_bytes) / (1024 * 1024)
            
            pdf_file = PDFFile(
                filename=uploaded_file.name,
                pdf_bytes=pdf_bytes,
                pages=pages,
                size_mb=size_mb,
                file_id=str(uuid.uuid4())[:8]
            )
            
            pdf_files.append(pdf_file)
            
        except Exception as e:
            st.error(f"❌ Error processing {uploaded_file.name}: {e}")
    
    return pdf_files

def upload_to_firestore(pdf_files: List[PDFFile], job_settings: dict):
    """Upload PDF files to Firestore"""
    if not FIRESTORE_OK or not pdf_files:
        return False
    
    try:
        job_id = str(uuid.uuid4())[:12]
        total_files = len(pdf_files)
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for i, pdf_file in enumerate(pdf_files):
            status_text.text(f"Uploading {pdf_file.filename}...")
            
            # Convert to base64 for storage
            b64_data = base64.b64encode(pdf_file.pdf_bytes).decode('utf-8')
            
            # Prepare document data
            doc_data = {
                "file_id": pdf_file.file_id,
                "filename": pdf_file.filename,
                "pdf_data": b64_data,
                "pages": pdf_file.pages,
                "size_mb": pdf_file.size_mb,
                "sha256": sha256_bytes(pdf_file.pdf_bytes),
                "job_id": job_id,
                "user_id": st.session_state.user_id,
                "settings": job_settings,
                "timestamp": datetime.datetime.now(),
                "status": "uploaded"
            }
            
            # Upload to Firestore
            doc_ref = db.collection("pdf_files").document(pdf_file.file_id)
            doc_ref.set(doc_data)
            
            # Update progress
            progress = (i + 1) / total_files
            progress_bar.progress(progress)
        
        status_text.text("✅ All files uploaded successfully!")
        st.session_state.upload_status = f"Uploaded {total_files} files to job {job_id}"
        
        # Generate payment info
        generate_payment_info(pdf_files, job_settings, job_id)
        
        return True
        
    except Exception as e:
        st.error(f"❌ Upload failed: {e}")
        return False

def generate_payment_info(pdf_files: List[PDFFile], job_settings: dict, job_id: str):
    """Generate payment information"""
    try:
        # Calculate pricing
        total_pages = sum(pdf.pages for pdf in pdf_files)
        total_files = len(pdf_files)
        copies = job_settings.get("copies", 1)
        
        # Simple pricing logic
        price_per_page = 2.0  # ₹2 per page
        total_amount = total_pages * price_per_page * copies
        min_charge = 20.0
        final_amount = max(total_amount, min_charge)
        
        payment_info = {
            "job_id": job_id,
            "total_files": total_files,
            "total_pages": total_pages,
            "copies": copies,
            "amount": round(final_amount, 2),
            "currency": "INR",
            "upi_id": "example@upi",  # Replace with actual UPI ID
            "status": "pending"
        }
        
        st.session_state.payment_info = payment_info
        
    except Exception as e:
        logger.error(f"Payment info generation failed: {e}")

# --------- Payment Functions ----------
def handle_payment():
    """Handle payment process"""
    payment_info = st.session_state.payment_info
    if not payment_info:
        return
    
    amount = payment_info["amount"]
    upi_id = payment_info["upi_id"]
    job_id = payment_info["job_id"]
    
    # Generate UPI URI
    upi_uri = generate_upi_uri(upi_id, amount, f"PDF Job {job_id}")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 💳 Payment Details")
        st.write(f"**Job ID:** {job_id}")
        st.write(f"**Files:** {payment_info['total_files']}")
        st.write(f"**Pages:** {payment_info['total_pages']}")
        st.write(f"**Copies:** {payment_info['copies']}")
        st.write(f"**Amount:** ₹{amount}")
        
        if st.button("💳 Pay Now", type="primary", use_container_width=True):
            try:
                webbrowser.open(upi_uri)
            except:
                pass
            st.success("🚀 Payment app should open automatically!")
            st.markdown(f"**UPI Link:** [Pay ₹{amount}]({upi_uri})")
    
    with col2:
        if QR_AVAILABLE:
            try:
                qr = qrcode.QRCode(version=1, box_size=6, border=2)
                qr.add_data(upi_uri)
                qr.make(fit=True)
                
                qr_img = qr.make_image(fill_color="black", back_color="white")
                
                img_buffer = io.BytesIO()
                qr_img.save(img_buffer, format='PNG')
                img_buffer.seek(0)
                
                st.image(img_buffer, width=200, caption="Scan to Pay")
                
            except Exception as e:
                st.error(f"QR generation failed: {e}")

# --------- Main UI ----------

# Sidebar
with st.sidebar:
    st.title("📋 System Status")
    
    st.write(f"**User ID:** {st.session_state.user_id}")
    st.write(f"**Firestore:** {'✅ Connected' if FIRESTORE_OK else '❌ Disconnected'}")
    st.write(f"**PDF Reader:** {'✅ Available' if PDF_READER_AVAILABLE else '⚠️ Limited'}")
    st.write(f"**QR Codes:** {'✅ Available' if QR_AVAILABLE else '❌ Disabled'}")
    
    if st.session_state.upload_status:
        st.success(st.session_state.upload_status)

# Main content
st.markdown('<div class="info-box">📄 <strong>PDF Upload Service</strong><br>Upload your PDF files for processing and storage.</div>', unsafe_allow_html=True)

# File upload section
if FIRESTORE_OK:
    uploaded_files = st.file_uploader(
        "Choose PDF files",
        type=['pdf'],
        accept_multiple_files=True,
        help="Select one or more PDF files to upload"
    )
    
    if uploaded_files:
        # Process files
        with st.spinner("📄 Processing PDF files..."):
            pdf_files = process_pdf_files(uploaded_files)
            st.session_state.pdf_files = pdf_files
        
        if pdf_files:
            # Display file information
            st.markdown("#### 📋 PDF Files Summary")
            
            total_pages = sum(pdf.pages for pdf in pdf_files)
            total_size = sum(pdf.size_mb for pdf in pdf_files)
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Files", len(pdf_files))
            with col2:
                st.metric("Total Pages", total_pages)
            with col3:
                st.metric("Total Size", f"{total_size:.1f} MB")
            
            # File details
            for i, pdf_file in enumerate(pdf_files):
                with st.container():
                    col1, col2, col3, col4 = st.columns([3, 1, 1, 1])
                    
                    with col1:
                        st.write(f"**{pdf_file.filename}**")
                    with col2:
                        st.write(f"{pdf_file.pages} pages")
                    with col3:
                        st.write(f"{pdf_file.size_mb:.1f} MB")
                    with col4:
                        # PDF preview
                        b64_pdf = base64.b64encode(pdf_file.pdf_bytes).decode('utf-8')
                        if st.button("👁️", key=f"preview_{i}", help="Preview PDF"):
                            pdf_display = f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="400"></iframe>'
                            st.markdown(pdf_display, unsafe_allow_html=True)
            
            # Job settings
            st.markdown("#### ⚙️ Job Settings")
            col1, col2 = st.columns(2)
            
            with col1:
                copies = st.number_input("Number of Copies", min_value=1, max_value=10, value=1)
            with col2:
                quality = st.selectbox("Print Quality", ["Standard", "High", "Draft"])
            
            job_settings = {
                "copies": copies,
                "quality": quality,
                "timestamp": datetime.datetime.now().isoformat()
            }
            
            # Upload button
            if st.button("🚀 Upload to Firestore", type="primary", use_container_width=True):
                success = upload_to_firestore(pdf_files, job_settings)
                if success:
                    st.balloons()

else:
    st.error("❌ Firestore not available. Please configure Firebase credentials.")

# Payment section
if st.session_state.payment_info and not st.session_state.get('payment_complete'):
    st.markdown("---")
    st.markdown("### 💰 Payment Required")
    handle_payment()
    
    if st.button("✅ Mark Payment Complete", type="primary"):
        st.session_state.payment_complete = True
        st.session_state.payment_info = None
        st.success("🎉 Payment completed! Your PDFs have been processed.")
        st.balloons()

# Reset button
if st.session_state.pdf_files or st.session_state.payment_info:
    if st.button("🔄 Start New Upload"):
        st.session_state.pdf_files = []
        st.session_state.payment_info = None
        st.session_state.upload_status = ""
        st.session_state.payment_complete = False
        st.rerun()

# Setup instructions
with st.expander("⚙️ Setup Instructions"):
    st.markdown("""
    ### Requirements
    ```
    streamlit
    firebase-admin
    pypdf  # or PyPDF2
    qrcode[pil]  # optional, for QR codes
    pillow
    ```
    
    ### Firebase Setup
    1. Create a Firebase project
    2. Enable Firestore Database
    3. Create a service account key
    4. Add the JSON key to Streamlit Secrets as `firebase_service_account`
    
    ### Features
    - ✅ PDF-only uploads
    - ✅ Page counting and file info
    - ✅ Firestore cloud storage
    - ✅ Payment integration with UPI
    - ✅ PDF preview capability
    - ✅ Simple and clean interface
    """)

# Footer
st.markdown("---")
st.markdown(
    "<div style='text-align: center; color: #666;'>"
    "📄 <strong>PDF Upload Service</strong> - Streamlined PDF processing"
    "</div>", 
    unsafe_allow_html=True
)
