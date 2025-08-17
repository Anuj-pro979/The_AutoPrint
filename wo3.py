# wo3_autoprint_firestore_sender_fixed.py — Enhanced Streamlit sender with fixes
# Run: streamlit run wo3_autoprint_firestore_sender_fixed.py

import streamlit as st
import streamlit.components.v1 as components
import os
import tempfile
import base64
import time
import json
import logging
import traceback
import shutil
import subprocess
import platform
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from fpdf import FPDF
from PIL import Image
from pathlib import Path
import hashlib
import datetime
import uuid
import webbrowser
import threading
import io
import queue
import zlib
from urllib.parse import quote_plus

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# Optional PDF page counter
try:
    from PyPDF2 import PdfReader
    PDF_READER_AVAILABLE = True
except Exception:
    try:
        from pypdf import PdfReader
        PDF_READER_AVAILABLE = True
    except Exception:
        PdfReader = None
        PDF_READER_AVAILABLE = False

# Optional QR generation
try:
    import qrcode
    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False

# Optional auto-refresh helper
try:
    from streamlit_autorefresh import st_autorefresh
    AUTORELOAD_AVAILABLE = True
except Exception:
    AUTORELOAD_AVAILABLE = False

# --------- Logging ----------
LOGFILE = os.path.join(tempfile.gettempdir(), f"autoprint_{int(time.time())}.log")
logger = logging.getLogger("autoprint")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = logging.FileHandler(LOGFILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s — %(levelname)s — %(message)s"))
    logger.addHandler(fh)

def log(msg: str, level: str = "info"):
    if level == "debug":
        logger.debug(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.info(msg)
    # Also log to console for debugging
    print(f"[{level.upper()}] {msg}")

# --------- Utilities ----------
def abspath(p: str) -> str:
    return os.path.abspath(p)

def safe_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
            log(f"Removed temporary file: {path}", "debug")
    except Exception as e:
        log(f"safe_remove({path}) failed: {e}", "warning")

def find_executable(names):
    for name in names:
        if os.path.exists(name):
            log(f"Found executable: {name}", "debug")
            return name
        path = shutil.which(name)
        if path:
            log(f"Found executable in PATH: {path}", "debug")
            return path
    log(f"No executable found for: {names}", "warning")
    return None

def run_subprocess(cmd: List[str], timeout: int = 60):
    try:
        log(f"Running command: {' '.join(cmd)}", "debug")
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        log(f"Command succeeded: {out[:200]}...", "debug")
        return True, out
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "") + (e.stderr or "")
        out += f"\nexit:{e.returncode}"
        log(f"Command failed: {out}", "error")
        return False, out
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        out += f"\nTimeout after {timeout}s"
        log(f"Command timed out: {out}", "error")
        return False, out
    except FileNotFoundError as e:
        log(f"Command not found: {e}", "error")
        return False, str(e)
    except Exception as e:
        log(f"Unexpected command error: {e}", "error")
        return False, str(e)

def system_is_headless() -> bool:
    try:
        if platform.system() in ("Linux", "Darwin"):
            return os.environ.get("DISPLAY", "") == ""
        elif platform.system() == "Windows":
            session = os.environ.get("SESSIONNAME", "")
            if not session or session.upper().startswith("SERVICE"):
                return True
            return False
    except Exception:
        return True
    return False

def retry_with_backoff(func, attempts=3, initial_delay=0.5, factor=2.0, *args, **kwargs):
    delay = initial_delay
    last_exc = None
    for i in range(attempts):
        try:
            log(f"Attempt {i+1}/{attempts} for {func.__name__}", "debug")
            result = func(*args, **kwargs)
            log(f"Function {func.__name__} succeeded on attempt {i+1}", "debug")
            return result
        except Exception as e:
            last_exc = e
            log(f"Attempt {i+1}/{attempts} failed for {func.__name__}: {e}", "warning")
            logger.debug(traceback.format_exc())
            if i < attempts - 1:  # Don't sleep on last attempt
                time.sleep(delay)
                delay *= factor
    log(f"All {attempts} attempts failed for {func.__name__}", "error")
    if last_exc:
        raise last_exc
    return None

# --------- Data classes ----------
@dataclass
class PrintSettings:
    copies: int = 1
    color_mode: str = "Color"
    duplex: str = "Single-sided"
    paper_size: str = "A4"
    orientation: str = "Portrait"
    quality: str = "High"
    collate: bool = True
    staple: bool = False

@dataclass
class ConvertedFile:
    orig_name: str
    pdf_name: str
    pdf_bytes: bytes
    settings: PrintSettings
    original_bytes: Optional[bytes] = None

# --------- Enhanced FileConverter ----------
class FileConverter:
    SUPPORTED_TEXT_EXTENSIONS = {'.txt', '.md', '.rtf', '.html', '.htm'}
    SUPPORTED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp'}
    LIBREOFFICE_TIMEOUT = 120  # Increased timeout
    PANDOC_TIMEOUT = 90

    @classmethod
    def convert_text_to_pdf_bytes(cls, file_content: bytes, encoding='utf-8') -> Optional[bytes]:
        try:
            log("Starting text to PDF conversion", "debug")
            # Try different encodings
            text = None
            for enc in ['utf-8', 'latin-1', 'cp1252']:
                try:
                    text = file_content.decode(enc, errors='replace')
                    log(f"Successfully decoded text with {enc}", "debug")
                    break
                except Exception as e:
                    log(f"Failed to decode with {enc}: {e}", "debug")
                    continue
            
            if not text:
                log("Failed to decode text file", "error")
                return None

            # Create PDF with better formatting
            pdf = FPDF()
            pdf.add_page()
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.set_font("Helvetica", size=10)
            
            # Handle text line by line
            lines = text.splitlines()
            log(f"Processing {len(lines)} lines of text", "debug")
            
            for line_num, line in enumerate(lines):
                try:
                    # Handle long lines
                    if len(line) > 200:
                        line = line[:197] + "..."
                    # Replace problematic characters
                    line = line.replace('\t', '    ')  # Replace tabs
                    pdf.cell(0, 5, txt=line, ln=1)
                except Exception as e:
                    log(f"Error processing line {line_num}: {e}", "warning")
                    # Skip problematic lines
                    continue
            
            result = pdf.output(dest='S')
            if isinstance(result, str):
                result = result.encode('latin-1')
            log("Text to PDF conversion successful", "debug")
            return result
            
        except Exception as e:
            log(f"convert_text_to_pdf_bytes failed: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def convert_image_to_pdf_bytes(cls, file_content: bytes) -> Optional[bytes]:
        try:
            log("Starting image to PDF conversion", "debug")
            from io import BytesIO
            
            # Validate image data
            if len(file_content) < 100:
                log("Image file too small, likely corrupted", "error")
                return None
                
            with Image.open(BytesIO(file_content)) as img:
                log(f"Image size: {img.size}, mode: {img.mode}", "debug")
                
                # Resize if too large (memory optimization)
                max_size = 2000
                if img.size[0] > max_size or img.size[1] > max_size:
                    log(f"Resizing image from {img.size}", "debug")
                    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                    log(f"Resized to: {img.size}", "debug")
                
                # Convert to RGB if necessary
                if img.mode != 'RGB':
                    log(f"Converting from {img.mode} to RGB", "debug")
                    img = img.convert('RGB')
                
                # Save as PDF
                out = BytesIO()
                img.save(out, format='PDF', quality=85)
                result = out.getvalue()
                log(f"Image to PDF conversion successful, size: {len(result)} bytes", "debug")
                return result
                
        except Exception as e:
            log(f"convert_image_to_pdf_bytes failed: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def convert_docx_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        input_path = abspath(input_path)
        log(f"Starting DOCX conversion: {input_path}", "debug")
        
        if not os.path.exists(input_path):
            log(f"Input file does not exist: {input_path}", "error")
            return None
            
        out_pdf = os.path.join(tempfile.gettempdir(), f"docx_out_{int(time.time()*1000)}_{os.getpid()}.pdf")
        headless = system_is_headless()
        log(f"System headless: {headless}, output path: {out_pdf}", "debug")

        # Try LibreOffice first (most reliable)
        soffice = find_executable([
            "soffice", "libreoffice",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            "/usr/bin/libreoffice", "/opt/libreoffice/program/soffice"
        ])
        
        if soffice:
            try:
                log("Trying LibreOffice conversion", "debug")
                def _try_libre():
                    cmd = [
                        soffice, 
                        "--headless", 
                        "--invisible",
                        "--nodefault",
                        "--nolockcheck",
                        "--nologo",
                        "--norestore",
                        "--convert-to", "pdf", 
                        "--outdir", os.path.dirname(out_pdf), 
                        input_path
                    ]
                    ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                    log(f"LibreOffice command result: {ok}, output: {out[:200]}...", "debug")
                    
                    # LibreOffice creates file with same base name
                    expected = os.path.join(
                        os.path.dirname(out_pdf), 
                        os.path.splitext(os.path.basename(input_path))[0] + ".pdf"
                    )
                    log(f"Looking for output file: {expected}", "debug")
                    
                    if os.path.exists(expected):
                        if expected != out_pdf:
                            log(f"Moving {expected} to {out_pdf}", "debug")
                            try:
                                os.replace(expected, out_pdf)
                            except Exception as e:
                                log(f"Failed to move file: {e}", "warning")
                                # Copy instead
                                shutil.copy2(expected, out_pdf)
                                safe_remove(expected)
                        return os.path.exists(out_pdf)
                    else:
                        log(f"Expected output file not found: {expected}", "warning")
                        # Sometimes LibreOffice creates files in current directory
                        alt_path = os.path.join(
                            os.getcwd(),
                            os.path.splitext(os.path.basename(input_path))[0] + ".pdf"
                        )
                        if os.path.exists(alt_path):
                            log(f"Found alternative output: {alt_path}", "debug")
                            shutil.move(alt_path, out_pdf)
                            return True
                    return False
                
                ok = retry_with_backoff(_try_libre, attempts=2)
                if ok and os.path.exists(out_pdf):
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    log(f"LibreOffice conversion successful, size: {len(data)} bytes", "debug")
                    safe_remove(out_pdf)
                    return data
                else:
                    log("LibreOffice conversion failed", "warning")
                    
            except Exception as e:
                log(f"LibreOffice conversion error: {e}", "warning")
                logger.debug(traceback.format_exc())

        # Try docx2pdf if not headless
        if not headless:
            try:
                import docx2pdf
                log("Trying docx2pdf conversion", "debug")
                
                def _try_docx2pdf():
                    try:
                        # Try newer API first
                        docx2pdf.convert(input_path, out_pdf)
                    except TypeError:
                        # Fallback to older API
                        docx2pdf.convert(input_path, os.path.dirname(out_pdf))
                        expected = os.path.join(
                            os.path.dirname(out_pdf), 
                            os.path.splitext(os.path.basename(input_path))[0] + ".pdf"
                        )
                        if os.path.exists(expected) and expected != out_pdf:
                            os.replace(expected, out_pdf)
                    return os.path.exists(out_pdf)
                
                ok = retry_with_backoff(_try_docx2pdf, attempts=2)
                if ok and os.path.exists(out_pdf):
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    log(f"docx2pdf conversion successful, size: {len(data)} bytes", "debug")
                    safe_remove(out_pdf)
                    return data
                    
            except ImportError:
                log("docx2pdf not available", "debug")
            except Exception as e:
                log(f"docx2pdf conversion failed: {e}", "warning")

        log("All DOCX conversion methods failed", "error")
        safe_remove(out_pdf)
        return None

    @classmethod
    def convert_pptx_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        input_path = abspath(input_path)
        log(f"Starting PPTX conversion: {input_path}", "debug")
        
        if not os.path.exists(input_path):
            log(f"Input file does not exist: {input_path}", "error")
            return None
            
        out_pdf = os.path.join(tempfile.gettempdir(), f"pptx_out_{int(time.time()*1000)}_{os.getpid()}.pdf")

        # Try LibreOffice first
        soffice = find_executable([
            "soffice", "libreoffice", "/usr/bin/libreoffice", "/opt/libreoffice/program/soffice"
        ])
        
        if soffice:
            try:
                log("Trying LibreOffice PPTX conversion", "debug")
                def _try_libre():
                    cmd = [
                        soffice, 
                        "--headless", 
                        "--invisible",
                        "--nodefault",
                        "--nolockcheck",
                        "--nologo",
                        "--norestore",
                        "--convert-to", "pdf", 
                        "--outdir", os.path.dirname(out_pdf), 
                        input_path
                    ]
                    ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                    expected = os.path.join(
                        os.path.dirname(out_pdf), 
                        os.path.splitext(os.path.basename(input_path))[0] + ".pdf"
                    )
                    if os.path.exists(expected):
                        if expected != out_pdf:
                            os.replace(expected, out_pdf)
                        return os.path.exists(out_pdf)
                    return False
                
                ok = retry_with_backoff(_try_libre, attempts=2)
                if ok and os.path.exists(out_pdf):
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    log(f"LibreOffice PPTX conversion successful, size: {len(data)} bytes", "debug")
                    safe_remove(out_pdf)
                    return data
                    
            except Exception as e:
                log(f"LibreOffice PPTX conversion failed: {e}", "warning")

        log("All PPTX conversion methods failed", "error")
        safe_remove(out_pdf)
        return None

    @classmethod
    def convert_generic_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        log(f"Starting generic conversion: {input_path}", "debug")
        out_pdf = os.path.join(tempfile.gettempdir(), f"generic_out_{int(time.time()*1000)}_{os.getpid()}.pdf")
        
        # Try LibreOffice
        soffice = find_executable(["soffice", "libreoffice", "/usr/bin/libreoffice"])
        if soffice:
            try:
                cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(out_pdf), input_path]
                ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                expected = os.path.join(os.path.dirname(out_pdf), os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                if os.path.exists(expected):
                    if expected != out_pdf:
                        os.replace(expected, out_pdf)
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    log(f"Generic LibreOffice conversion successful", "debug")
                    return data
            except Exception as e:
                log(f"LibreOffice generic failed: {e}", "warning")
        
        safe_remove(out_pdf)
        return None

    @classmethod
    def convert_uploaded_file_to_pdf_bytes(cls, uploaded_file) -> Optional[bytes]:
        if not uploaded_file:
            log("No uploaded file provided", "error")
            return None
            
        log(f"Starting conversion for: {uploaded_file.name}", "info")
        suffix = os.path.splitext(uploaded_file.name)[1].lower()
        content = uploaded_file.getvalue()
        
        if len(content) == 0:
            log(f"Empty file: {uploaded_file.name}", "error")
            return None
        
        log(f"File size: {len(content)} bytes, extension: {suffix}", "debug")
        
        try:
            # PDF files - return as-is
            if suffix == ".pdf":
                log("PDF file detected, returning as-is", "debug")
                return content
            
            # Text files
            if suffix in cls.SUPPORTED_TEXT_EXTENSIONS:
                log("Text file detected", "debug")
                return cls.convert_text_to_pdf_bytes(content)
            
            # Image files
            if suffix in cls.SUPPORTED_IMAGE_EXTENSIONS:
                log("Image file detected", "debug")
                return cls.convert_image_to_pdf_bytes(content)
            
            # Office documents - need temporary files
            if suffix in (".docx", ".pptx"):
                log(f"Office document detected: {suffix}", "debug")
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                    tf.write(content)
                    tf.flush()
                    tmpname = tf.name
                    log(f"Created temporary file: {tmpname}", "debug")
                
                try:
                    if suffix == ".docx":
                        result = cls.convert_docx_to_pdf_bytes(tmpname)
                    else:
                        result = cls.convert_pptx_to_pdf_bytes(tmpname)
                    
                    if result:
                        log(f"Office document conversion successful, size: {len(result)} bytes", "debug")
                    else:
                        log("Office document conversion failed", "error")
                    return result
                finally:
                    safe_remove(tmpname)
            
            # Generic files - try LibreOffice
            log("Generic file - trying LibreOffice", "debug")
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                tf.write(content)
                tf.flush()
                tmpname = tf.name
            
            try:
                return cls.convert_generic_to_pdf_bytes(tmpname)
            finally:
                safe_remove(tmpname)
                
        except Exception as e:
            log(f"convert_uploaded_file_to_pdf_bytes failed for {uploaded_file.name}: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

# --------- Page counting helper ----------
def count_pdf_pages(blob: Optional[bytes]) -> int:
    if not blob:
        log("No PDF data provided for page counting", "debug")
        return 1
        
    if not PDF_READER_AVAILABLE:
        log("PDF reader not available, assuming 1 page", "debug")
        return 1
        
    try:
        stream = io.BytesIO(blob)
        reader = PdfReader(stream)
        pages = len(reader.pages)
        log(f"PDF has {pages} pages", "debug")
        return pages
    except Exception as e:
        log(f"count_pdf_pages failed: {e}", "warning")
        logger.debug(traceback.format_exc())
        return 1

# --------- Enhanced Firestore utilities ----------
COLLECTION = st.secrets.get("collection_name", "files") if st.secrets else "files"
CHUNK_TEXT_SIZE = 900_000  # Reduced chunk size for better reliability
MAX_BATCH_WRITE = 200      # Reduced batch size

def init_db_from_secrets():
    try:
        sa_json = st.secrets.get("firebase_service_account") if st.secrets else None
        if sa_json:
            sa = json.loads(sa_json)
            log("Loaded service account from Streamlit secrets", "debug")
        else:
            fallback_path = st.secrets.get("service_account_file") if st.secrets else None
            if not fallback_path:
                raise RuntimeError("Provide firebase_service_account in Streamlit secrets or service_account_file path.")
            with open(fallback_path, "r", encoding="utf-8") as f:
                sa = json.load(f)
            log(f"Loaded service account from file: {fallback_path}", "debug")
        
        # Fix private key formatting
        if "private_key" in sa and isinstance(sa["private_key"], str):
            sa["private_key"] = sa["private_key"].replace("\\n", "\n")
        
        # Initialize Firebase
        try:
            app = firebase_admin.get_app()
            log("Firebase app already initialized", "debug")
        except ValueError:
            cred = credentials.Certificate(sa)
            app = firebase_admin.initialize_app(cred)
            log("Firebase app initialized successfully", "debug")
        
        return firestore.client(app=app)
        
    except Exception as e:
        log(f"Firebase initialization failed: {e}", "error")
        raise

# Initialize Firestore client
try:
    db = init_db_from_secrets()
    log("Firestore client initialized successfully", "info")
except Exception as e:
    st.error(f"❌ Firebase init failed: {e}")
    st.info("Please check your Firebase configuration in Streamlit secrets.")
    st.stop()

# --------- Enhanced crypto / encode functions ----------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def compress_and_encode_bytes(b: bytes) -> str:
    try:
        compressed = zlib.compress(b, level=6)  # Good balance of speed/compression
        encoded = base64.b64encode(compressed).decode("utf-8")
        log(f"Compressed {len(b)} bytes to {len(compressed)} bytes ({len(encoded)} encoded)", "debug")
        return encoded
    except Exception as e:
        log(f"Compression failed: {e}", "error")
        raise

def chunk_text(text: str, size: int = CHUNK_TEXT_SIZE) -> List[str]:
    chunks = [text[i:i+size] for i in range(0, len(text), size)]
    log(f"Split text into {len(chunks)} chunks of max size {size}", "debug")
    return chunks

# Thread-safe queue for listener updates
ACK_QUEUE = queue.Queue()

# --------- Enhanced file upload function ----------
def send_file_to_firestore(file_bytes: bytes, file_name: str, user_name: str = "", user_id: str = "") -> Tuple[str, int]:
    try:
        log(f"Starting upload for file: {file_name}, size: {len(file_bytes)} bytes", "info")
        
        file_sha = sha256_bytes(file_bytes)
        log(f"File SHA256: {file_sha}", "debug")
        
        # Compress and encode
        full_b64 = compress_and_encode_bytes(file_bytes)
        chunks = chunk_text(full_b64, CHUNK_TEXT_SIZE)
        total_chunks = len(chunks)
        file_id = str(uuid.uuid4())
        
        log(f"File will be uploaded as {total_chunks} chunks with ID: {file_id}", "info")
        
        # Upload chunks in batches
        batch = db.batch()
        written = 0
        
        for idx, piece in enumerate(chunks):
            try:
                doc_ref = db.collection(COLLECTION).document(f"{file_id}_{idx}")
                batch.set(doc_ref, {
                    "file_id": file_id,
                    "file_name": file_name,
                    "chunk_index": idx,
                    "total_chunks": total_chunks,
                    "data": piece,
                    "created_at": firestore.SERVER_TIMESTAMP
                })
                written += 1
                
                # Commit batch when it reaches max size
                if written % MAX_BATCH_WRITE == 0:
                    log(f"Committing batch of {MAX_BATCH_WRITE} chunks", "debug")
                    batch.commit()
                    batch = db.batch()
                    
            except Exception as e:
                log(f"Error adding chunk {idx} to batch: {e}", "error")
                raise
        
        # Commit remaining chunks
        if written % MAX_BATCH_WRITE != 0:
            log(f"Committing final batch of {written % MAX_BATCH_WRITE} chunks", "debug")
            batch.commit()

        # Create metadata document with proper error handling
        log("Creating file metadata document", "debug")
        meta_ref = db.collection(COLLECTION).document(f"{file_id}_meta")
        meta_payload = {
            "file_id": file_id,
            "file_name": file_name,
            "total_chunks": total_chunks,  # This should match the actual chunks created
            "sha256": file_sha,
            "size_bytes": len(file_bytes),
            "uploaded_at": firestore.SERVER_TIMESTAMP,
            "user_name": user_name or "",
            "user_id": user_id or "",
            "status": "uploaded"
        }
        meta_ref.set(meta_payload)
        
        log(f"File upload completed: {file_id}, chunks: {total_chunks}", "info")
        return file_id, total_chunks
        
    except Exception as e:
        log(f"File upload failed for {file_name}: {e}", "error")
        logger.debug(traceback.format_exc())
        raise

# --------- Enhanced job creation ----------
def send_job_to_firestore(files: List[Dict[str, Any]], user_name: str = "", user_id: str = "") -> Tuple[str, List[Dict[str, Any]]]:
    if not files:
        raise ValueError("No files to upload")
        
    job_id = str(uuid.uuid4())
    job_files = []
    total_files = len(files)
    
    log(f"Creating job {job_id} with {total_files} files", "info")
    
    # Create progress bar
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    try:
        for idx, f in enumerate(files):
            file_bytes = f.get("file_bytes") or b""
            file_name = f.get("file_name") or f"file_{int(time.time())}.pdf"
            pages = f.get("pages") or count_pdf_pages(file_bytes)
            settings = f.get("settings") or {}
            
            log(f"Processing file {idx+1}/{total_files}: {file_name}", "info")
            status_text.text(f"Uploading file {idx+1}/{total_files}: {file_name}")
            
            # Upload individual file
            try:
                fid, total_chunks = send_file_to_firestore(
                    file_bytes, file_name, user_name=user_name, user_id=user_id
                )
                
                job_files.append({
                    "file_id": fid,
                    "file_name": file_name,
                    "size_bytes": len(file_bytes),
                    "pages": pages,
                    "total_chunks": total_chunks,
                    "settings": settings,
                    "will_send_converted": True
                })
                
                log(f"Successfully uploaded file: {file_name} as {fid}", "debug")
                
            except Exception as e:
                log(f"Failed to upload file {file_name}: {e}", "error")
                st.error(f"Failed to upload {file_name}: {e}")
                # Continue with other files
                continue
            
            # Update progress
            progress = int(((idx + 1) / total_files) * 100)
            progress_bar.progress(progress)

        # Create job manifest
        log("Creating job manifest", "debug")
        job_meta = {
            "job_id": job_id,
            "file_count": len(job_files),
            "files": job_files,
            "user_name": user_name or "",
            "user_id": user_id or "",
            "timestamp": firestore.SERVER_TIMESTAMP,
            "transfer_mode": "file_share",
            "status": "uploaded"
        }
        
        db.collection(COLLECTION).document(f"{job_id}_meta").set(job_meta)
        log(f"Job manifest created: {job_id}", "info")

        # Update health check
        try:
            db.collection("health_check").document("last_job").set({
                "job_id": job_id, 
                "ts": firestore.SERVER_TIMESTAMP,
                "user_id": user_id
            })
        except Exception as e:
            log(f"Health check update failed: {e}", "warning")

        progress_bar.empty()
        status_text.empty()
        
        return job_id, job_files
        
    except Exception as e:
        log(f"Job creation failed: {e}", "error")
        progress_bar.empty()
        status_text.empty()
        raise

# --------- Enhanced Listener Functions ----------
def attach_job_listener(job_id: str):
    try:
        doc_ref = db.collection(COLLECTION).document(f"{job_id}_meta")
        
        def callback(doc_snapshot, changes, read_time):
            try:
                # Handle different snapshot types
                doc = None
                if isinstance(doc_snapshot, list) and len(doc_snapshot) > 0:
                    doc = doc_snapshot[0]
                else:
                    doc = doc_snapshot
                    
                if doc is None or not doc.exists:
                    log("Job listener: document not found or deleted", "debug")
                    return

                data = doc.to_dict() or {}
                log(f"Job listener update: {list(data.keys())}", "debug")
                
                # Handle payment info
                if "payinfo" in data:
                    payinfo = data.get("payinfo")
                    log(f"Received payment info: {payinfo}", "info")
                    ACK_QUEUE.put(("payinfo", payinfo))
                    
                    # Check if payment is already completed
                    if isinstance(payinfo, dict):
                        status = payinfo.get("status", "").lower()
                        if payinfo.get("paid") or status in ("paid", "completed", "received"):
                            log("Payment already completed", "info")
                            ACK_QUEUE.put(("payment", {"job_id": job_id, "payinfo": payinfo}))

                # Handle direct payment fields
                if data.get("payment_received") is True or data.get("payment_status") in ("paid", "completed", "received"):
                    log("Direct payment confirmation received", "info")
                    ACK_QUEUE.put(("payment", {"job_id": job_id, "payload": data}))

                # Handle print acknowledgments
                if "final_acks" in data:
                    for ack in (data.get("final_acks") or []):
                        log(f"Received print ack: {ack}", "info")
                        ACK_QUEUE.put(("ack", ack))

                # Handle status updates
                if "status" in data:
                    status = data.get("status")
                    log(f"Job status update: {status}", "info")
                    ACK_QUEUE.put(("status", {"status": status, "job_id": job_id}))

            except Exception as e:
                log(f"Job listener callback error: {e}", "error")
                logger.debug(traceback.format_exc())

        listener = doc_ref.on_snapshot(callback)
        st.session_state["job_listener"] = listener
        log(f"Job listener attached for: {job_id}", "info")
        
    except Exception as e:
        log(f"Failed to attach job listener: {e}", "error")
        raise

def detach_job_listener():
    listener = st.session_state.get("job_listener")
    if listener:
        try:
            listener.unsubscribe()
            log("Job listener detached", "debug")
        except Exception as e:
            log(f"Error detaching job listener: {e}", "warning")
    st.session_state["job_listener"] = None

def attach_file_listener(file_id: str):
    try:
        doc_ref = db.collection(COLLECTION).document(f"{file_id}_meta")
        
        def callback(doc_snapshot, changes, read_time):
            try:
                doc = None
                if isinstance(doc_snapshot, list) and len(doc_snapshot) > 0:
                    doc = doc_snapshot[0]
                else:
                    doc = doc_snapshot
                    
                if doc is None or not doc.exists:
                    return

                data = doc.to_dict() or {}
                log(f"File listener update for {file_id}: {list(data.keys())}", "debug")
                
                # Handle payment info
                if "payinfo" in data:
                    payinfo = data.get("payinfo")
                    ACK_QUEUE.put(("payinfo", payinfo))
                    
                    if isinstance(payinfo, dict):
                        if payinfo.get("paid") or payinfo.get("status", "").lower() in ("paid", "completed", "received"):
                            ACK_QUEUE.put(("payment", {"file_id": file_id, "payinfo": payinfo}))

                # Handle payment confirmation
                if data.get("payment_received") is True or data.get("payment_status") in ("paid", "completed", "received"):
                    ACK_QUEUE.put(("payment", {"file_id": file_id, "payload": data}))

            except Exception as e:
                log(f"File listener callback error: {e}", "error")
                logger.debug(traceback.format_exc())

        listener = doc_ref.on_snapshot(callback)
        
        # Store in session state
        if "file_listeners" not in st.session_state:
            st.session_state["file_listeners"] = {}
        st.session_state["file_listeners"][file_id] = listener
        log(f"File listener attached for: {file_id}", "debug")
        
    except Exception as e:
        log(f"Failed to attach file listener for {file_id}: {e}", "error")

def detach_file_listeners():
    listeners = st.session_state.get("file_listeners", {})
    for file_id, listener in listeners.items():
        try:
            listener.unsubscribe()
            log(f"File listener detached for: {file_id}", "debug")
        except Exception as e:
            log(f"Error detaching file listener for {file_id}: {e}", "warning")
    st.session_state["file_listeners"] = {}

# --------- Enhanced Payment Functions ----------
def validate_payment_info(payinfo: dict) -> bool:
    """Validate payment information"""
    if not payinfo:
        log("Payment info is empty", "warning")
        return False
    
    upi_id = payinfo.get("owner_upi", "").strip()
    amount = payinfo.get("amount", 0)
    
    log(f"Validating payment info: UPI={upi_id}, Amount={amount}", "debug")
    
    # Validate UPI ID
    if not upi_id or "@" not in upi_id:
        log("Invalid UPI ID format", "warning")
        return False
    
    # Validate amount
    try:
        amount = float(amount)
        if amount <= 0:
            log("Invalid amount: must be positive", "warning")
            return False
    except (ValueError, TypeError):
        log("Invalid amount: not a number", "warning")
        return False
    
    log("Payment info validation successful", "debug")
    return True

def generate_upi_uri(upi_id: str, amount: float, note: str = None) -> str:
    """Generate UPI payment URI"""
    try:
        params = [
            f"pa={quote_plus(upi_id)}", 
            f"am={quote_plus(str(amount))}"
        ]
        if note:
            params.append(f"tn={quote_plus(note)}")
        
        uri = "upi://pay?" + "&".join(params)
        log(f"Generated UPI URI: {uri}", "debug")
        return uri
    except Exception as e:
        log(f"Error generating UPI URI: {e}", "error")
        return f"upi://pay?pa={upi_id}&am={amount}"

def show_enhanced_payment_ui(payinfo: dict):
    """Enhanced payment UI with online/offline options"""
    if not validate_payment_info(payinfo):
        st.error("❌ **Invalid payment information received**")
        st.write("Please contact support or try again.")
        if st.button("🔄 Cancel & Retry", key="cancel_invalid_payment"):
            cancel_payment()
        return

    # Extract payment details
    upi_id = payinfo.get("owner_upi", "")
    amount = float(payinfo.get("amount", 0))
    currency = payinfo.get("currency", "INR")
    file_name = payinfo.get("file_name", "Print Job")
    pages = payinfo.get("pages", "N/A")
    copies = payinfo.get("copies", 1)
    order_id = payinfo.get("order_id", "")

    log(f"Displaying payment UI: Amount={amount}, UPI={upi_id}", "info")

    # Payment header
    st.markdown("---")
    st.markdown("## 💳 **Payment Required**")
    
    # Payment details
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### 📄 **Job Details**")
        st.write(f"**📁 File:** {file_name}")
        st.write(f"**📑 Pages:** {pages}")
        st.write(f"**📇 Copies:** {copies}")
    
    with col2:
        st.markdown("### 💰 **Payment Details**")
        st.write(f"**💵 Amount:** ₹{amount:.2f} {currency}")
        st.write(f"**🔢 Order ID:** {order_id}")
        st.write(f"**💳 UPI ID:** {upi_id}")

    st.markdown("---")
    
    # Payment method selection
    st.markdown("### 🎯 **Choose Payment Method**")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 📱 **Pay Online (Recommended)**")
        st.markdown("""
        ✅ **Benefits:**
        • Instant payment via UPI apps
        • Automatic confirmation
        • QR code available
        • Secure transaction
        """)
        
        if st.button("💳 **Pay Online**", type="primary", use_container_width=True, key="pay_online_btn"):
            handle_online_payment(upi_id, amount, file_name, order_id)
    
    with col2:
        st.markdown("#### 💵 **Pay Offline**")
        st.markdown("""
        📝 **Process:**
        • Pay directly to shop owner
        • Manual confirmation
        • Cash or other methods
        • Show this screen to owner
        """)
        
        if st.button("💵 **Pay Offline**", use_container_width=True, key="pay_offline_btn"):
            handle_offline_payment(amount, currency, file_name)

    # Cancel option
    st.markdown("---")
    col1, col2, col3 = st.columns([1,1,1])
    with col2:
        if st.button("❌ **Cancel Payment**", key="cancel_payment_btn"):
            cancel_payment()

def handle_online_payment(upi_id: str, amount: float, file_name: str, order_id: str = ""):
    """Handle online UPI payment"""
    try:
        log(f"Processing online payment: {amount} to {upi_id}", "info")
        
        # Generate UPI URI
        note = f"Print: {file_name}"
        if order_id:
            note += f" (Order: {order_id})"
        upi_uri = generate_upi_uri(upi_id, amount, note)
        
        # Mark payment attempt
        mark_payment_attempt("upi_online")
        
        # Show payment interface
        st.markdown("### 📱 **Complete UPI Payment**")
        
        # Payment button with enhanced styling
        st.markdown(f"""
        <div style="text-align: center; padding: 25px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 15px; margin: 15px 0; box-shadow: 0 8px 32px rgba(102, 126, 234, 0.3);">
            <h2 style="color: white; margin-bottom: 20px; font-weight: 600;">💳 Pay ₹{amount:.2f}</h2>
            <p style="color: white; margin-bottom: 20px; opacity: 0.9;">Secure UPI Payment</p>
            <a href="{upi_uri}" target="_blank" style="display: inline-block; padding: 15px 40px; background: white; color: #667eea; text-decoration: none; border-radius: 30px; font-weight: bold; box-shadow: 0 4px 20px rgba(0,0,0,0.2); transition: transform 0.2s;">
                🚀 Open Payment App
            </a>
        </div>
        """, unsafe_allow_html=True)
        
        # QR Code
        if QR_AVAILABLE:
            try:
                col1, col2, col3 = st.columns([1,2,1])
                with col2:
                    qr = qrcode.QRCode(version=1, box_size=8, border=2)
                    qr.add_data(upi_uri)
                    qr.make(fit=True)
                    
                    img = qr.make_image(fill_color="black", back_color="white")
                    st.image(img, caption="📱 Scan with any UPI app", use_container_width=False, width=250)
                    log("QR code generated successfully", "debug")
            except Exception as e:
                log(f"QR code generation failed: {e}", "warning")
                st.info("QR code generation failed, please use the payment button above.")
        
        # Auto-open payment app
        try:
            webbrowser.open(upi_uri)
            log("Payment app opened automatically", "debug")
        except Exception as e:
            log(f"Failed to auto-open payment app: {e}", "debug")
        
        # Payment status and instructions
        st.info("🕐 **Payment in progress...**")
        st.markdown("""
        **📋 Instructions:**
        1. 📱 Click "Open Payment App" or scan the QR code
        2. 💳 Complete the payment in your UPI app (GPay, PhonePe, Paytm, etc.)
        3. ⏳ Wait for automatic confirmation (usually takes a few seconds)
        4. ✅ You'll see a success message once confirmed
        
        **💡 Tip:** Keep this tab open while making payment for instant confirmation!
        """)
        
        # Set waiting state
        st.session_state["waiting_for_payment"] = True
        st.session_state["process_complete"] = False
        
    except Exception as e:
        log(f"Online payment handling failed: {e}", "error")
        st.error(f"Payment setup failed: {e}")

def handle_offline_payment(amount: float, currency: str, file_name: str):
    """Handle offline payment"""
    try:
        log(f"Processing offline payment: {amount} {currency} for {file_name}", "info")
        
        # Mark offline payment
        mark_payment_attempt("offline")
        mark_payment_completed("offline")
        
        # Show success animation
        st.balloons()
        
        # Success message
        st.markdown(f"""
        <div style="text-align: center; padding: 30px; background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%); border-radius: 15px; color: white; margin: 20px 0; box-shadow: 0 8px 32px rgba(76, 175, 80, 0.3);">
            <h2 style="margin-bottom: 15px;">🎉 Offline Payment Selected!</h2>
            <h3 style="margin: 20px 0;">💵 Please pay ₹{amount:.2f} {currency} to the shop owner</h3>
            <p style="font-size: 18px; margin: 15px 0;">✅ Your print job is now queued for processing</p>
            <p style="font-size: 16px; margin: 15px 0; opacity: 0.9;">Show this screen to the shop owner as confirmation</p>
        </div>
        """, unsafe_allow_html=True)
        
        st.success("✅ **Thank you for using our service!**")
        st.markdown("""
        ### 📝 **Next Steps:**
        1. 💰 Pay the shop owner directly (cash or other accepted methods)
        2. 📱 Show this confirmation screen to the owner
        3. 🖨️ Your files will be printed shortly
        4. 📞 Contact the shop if you have any questions
        
        **📋 Order Details:**
        - File: {file_name}
        - Amount: ₹{amount:.2f} {currency}
        - Status: Ready for printing
        """.format(file_name=file_name, amount=amount, currency=currency))
        
        # Complete the process
        complete_payment_process()
        
    except Exception as e:
        log(f"Offline payment handling failed: {e}", "error")
        st.error(f"Payment processing failed: {e}")

def mark_payment_attempt(method: str):
    """Mark payment attempt in Firestore"""
    try:
        job_file_ids = st.session_state.get("current_file_ids", [])
        job_id = st.session_state.get("current_job_id")
        user_id = st.session_state.get("user_id", "")
        
        log(f"Marking payment attempt: method={method}, files={len(job_file_ids)}", "debug")
        
        # Update job manifest
        if job_id:
            try:
                db.collection(COLLECTION).document(f"{job_id}_meta").update({
                    "payment_attempted_by": user_id,
                    "payment_attempt_time": firestore.SERVER_TIMESTAMP,
                    "payment_method": method,
                    "payment_status": "attempted"
                })
                log(f"Payment attempt marked for job: {job_id}", "debug")
            except Exception as e:
                log(f"Failed to mark payment attempt for job {job_id}: {e}", "warning")
        
        # Update individual files
        for fid in job_file_ids:
            try:
                db.collection(COLLECTION).document(f"{fid}_meta").update({
                    "payment_attempted_by": user_id,
                    "payment_attempt_time": firestore.SERVER_TIMESTAMP,
                    "payment_method": method,
                    "payment_status": "attempted"
                })
            except Exception as e:
                log(f"Failed to mark payment attempt for file {fid}: {e}", "warning")
                
    except Exception as e:
        log(f"Failed to mark payment attempt: {e}", "error")

def mark_payment_completed(method: str):
    """Mark payment as completed"""
    try:
        job_file_ids = st.session_state.get("current_file_ids", [])
        job_id = st.session_state.get("current_job_id")
        user_id = st.session_state.get("user_id", "")
        
        log(f"Marking payment completed: method={method}, files={len(job_file_ids)}", "info")
        
        # Update job manifest
        if job_id:
            try:
                db.collection(COLLECTION).document(f"{job_id}_meta").update({
                    "payment_confirmed_by": user_id,
                    "payment_method": method,
                    "payment_time": firestore.SERVER_TIMESTAMP,
                    "payment_received": True,
                    "payment_status": "completed"
                })
                log(f"Payment completion marked for job: {job_id}", "info")
            except Exception as e:
                log(f"Failed to mark payment completion for job {job_id}: {e}", "error")
        
        # Update individual files
        for fid in job_file_ids:
            try:
                db.collection(COLLECTION).document(f"{fid}_meta").update({
                    "payment_confirmed_by": user_id,
                    "payment_method": method,
                    "payment_time": firestore.SERVER_TIMESTAMP,
                    "payment_received": True,
                    "payment_status": "completed"
                })
            except Exception as e:
                log(f"Failed to mark payment completion for file {fid}: {e}", "warning")
                
    except Exception as e:
        log(f"Failed to mark payment completion: {e}", "error")

def complete_payment_process():
    """Complete payment process and clean up"""
    log("Completing payment process", "info")
    
    # Update session state
    st.session_state["payinfo"] = None
    st.session_state["process_complete"] = True
    st.session_state["waiting_for_payment"] = False
    
    # Clean up listeners
    detach_file_listeners()
    detach_job_listener()
    
    # Clear job references but keep for display
    # st.session_state["current_job_id"] = None
    # st.session_state["current_file_ids"] = []
    
    set_status("Payment process completed successfully")

def cancel_payment():
    """Cancel payment process"""
    log("Payment cancelled by user", "info")
    
    # Clean up listeners
    detach_job_listener()
    detach_file_listeners()
    
    # Reset session state
    st.session_state["payinfo"] = None
    st.session_state["current_job_id"] = None
    st.session_state["current_file_ids"] = []
    st.session_state["waiting_for_payment"] = False
    st.session_state["process_complete"] = False
    
    st.warning("⚠️ Payment cancelled. You can start a new print job.")
    set_status("Payment cancelled")

# --------- Session State Initialization ----------
def init_session_state():
    """Initialize session state variables"""
    defaults = {
        'converted_files_pm': [],
        'converted_files_conv': [],
        'formatted_pdfs': {},
        'payinfo': None,
        'status': "",
        'process_complete': False,
        'user_name': "",
        'user_id': str(uuid.uuid4())[:8],
        'print_ack': None,
        'job_listener': None,
        'current_job_id': None,
        'current_file_ids': [],
        'waiting_for_payment': False,
        'file_listeners': {}
    }
    
    for key, default_value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default_value

def set_status(s):
    """Set status with timestamp"""
    timestamp = datetime.datetime.now().strftime('%H:%M:%S')
    st.session_state["status"] = f"{timestamp} - {s}"
    log(f"Status updated: {s}", "info")

def start_job_listener(job_id: str):
    """Start job listener and update session state"""
    detach_job_listener()
    st.session_state["current_job_id"] = job_id
    attach_job_listener(job_id)
    set_status(f"Listening for job updates: {job_id}")

def process_ack_queue():
    """Process acknowledgment queue"""
    changed = False
    processed = 0
    
    try:
        while processed < 10:  # Limit processing to avoid infinite loops
            try:
                typ, payload = ACK_QUEUE.get_nowait()
                processed += 1
                log(f"Processing ACK: type={typ}, payload keys={list(payload.keys()) if isinstance(payload, dict) else 'N/A'}", "debug")
                
                if typ == "payinfo":
                    st.session_state["payinfo"] = payload
                    set_status("Payment information received from printer")
                    changed = True
                    
                elif typ == "ack":
                    st.session_state["print_ack"] = payload
                    status = payload.get('status', 'unknown') if isinstance(payload, dict) else str(payload)
                    set_status(f"Print completed: {status}")
                    changed = True
                    
                elif typ == "status":
                    job_id = payload.get('job_id', '') if isinstance(payload, dict) else ''
                    status = payload.get('status', '') if isinstance(payload, dict) else ''
                    set_status(f"Job {job_id[:8]}... status: {status}")
                    changed = True
                    
                elif typ == "payment":
                    set_status("Payment confirmed by printer!")
                    st.success("✅ **Payment confirmed - thank you!**")
                    st.balloons()
                    complete_payment_process()
                    changed = True
                    
            except queue.Empty:
                break
                
    except Exception as e:
        log(f"Error processing ACK queue: {e}", "error")
    
    return changed

def send_multiple_files_firestore(converted_files: List[ConvertedFile], copies: int, color_mode: str):
    """Send multiple files to Firestore"""
    if not converted_files:
        st.error("No files selected to send.")
        return

    log(f"Sending {len(converted_files)} files to Firestore", "info")
    set_status("Preparing upload to Firestore...")
    
    files_payload = []
    total_bytes = 0
    
    for cf in converted_files:
        blob = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
        if len(blob) == 0:
            log(f"Skipping empty file: {cf.orig_name}", "warning")
            continue
            
        size = len(blob)
        pages = count_pdf_pages(blob)
        settings = {
            "copies": copies,
            "duplex": cf.settings.duplex,
            "colorMode": color_mode,
            "paperSize": cf.settings.paper_size,
            "orientation": cf.settings.orientation,
            "collate": cf.settings.collate
        }
        
        files_payload.append({
            "file_bytes": blob,
            "file_name": cf.pdf_name,
            "pages": pages,
            "settings": settings
        })
        total_bytes += size
        log(f"Prepared file: {cf.pdf_name}, size: {size} bytes, pages: {pages}", "debug")

    if not files_payload:
        st.error("No valid files to send.")
        return

    try:
        user_name = st.session_state.get("user_name", "")
        user_id = st.session_state.get("user_id", "")
        
        job_id, job_files = send_job_to_firestore(files_payload, user_name=user_name, user_id=user_id)
        
        set_status(f"Job uploaded successfully: {job_id}")
        st.success(f"✅ **Print job created:** `{job_id}`")
        log(f"Job created successfully: {job_id} with {len(job_files)} files", "info")
        
        # Record job and file IDs
        st.session_state["current_job_id"] = job_id
        st.session_state["current_file_ids"] = [f["file_id"] for f in job_files]
        
        # Start listeners
        start_job_listener(job_id)
        for f in job_files:
            try:
                attach_file_listener(f["file_id"])
            except Exception as e:
                log(f"Failed to attach file listener for {f['file_id']}: {e}", "warning")
        
        # Reset payment state
        st.session_state["payinfo"] = None
        st.session_state["print_ack"] = None
        st.session_state["process_complete"] = False
        st.session_state["waiting_for_payment"] = False
        
        log("Job upload and listener setup completed", "info")
        
    except Exception as e:
        st.error(f"❌ Upload failed: {e}")
        set_status("Upload failed")
        log(f"Job upload failed: {e}", "error")
        logger.debug(traceback.format_exc())
        return

# --------- Streamlit UI Configuration ----------
st.set_page_config(
    page_title="Autoprint Service (Enhanced)", 
    layout="wide", 
    page_icon="🖨️", 
    initial_sidebar_state="expanded"
)

# Custom CSS with enhanced styling
st.markdown("""
<style>
    .appview-container .main .block-container {
        padding-top: 10px; 
        padding-bottom: 10px;
    }
    .stButton>button {
        padding: 8px 12px; 
        font-weight: 500;
        transition: all 0.3s ease;
    }
    .stButton>button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }
    .stDownloadButton>button {
        padding: 8px 12px;
    }
    .stProgress {
        height: 16px;
    }
    
    /* Enhanced payment UI styles */
    .payment-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 20px;
        border-radius: 15px;
        color: white;
        text-align: center;
        margin: 15px 0;
        box-shadow: 0 8px 32px rgba(102, 126, 234, 0.3);
    }
    
    .success-card {
        background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
        padding: 20px;
        border-radius: 15px;
        color: white;
        text-align: center;
        margin: 15px 0;
        box-shadow: 0 8px 32px rgba(76, 175, 80, 0.3);
    }
    
    .status-info {
        background: linear-gradient(135deg, #17a2b8 0%, #138496 100%);
        padding: 15px;
        border-radius: 10px;
        color: white;
        margin: 10px 0;
    }
    
    .file-card {
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        padding: 15px;
        margin: 10px 0;
        background: #f8f9fa;
    }
    
    .metric-card {
        background: white;
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        text-align: center;
        margin: 5px;
    }
</style>
""", unsafe_allow_html=True)

# App Header
st.markdown("""
<div style="text-align: center; padding: 20px 0;">
    <h1 style="margin: 6px 0 8px 0; color: #333; font-weight: 600;">🖨️ Autoprint Service</h1>
    <p style="color: #666; font-size: 18px; margin: 0;">Professional Document Printing with UPI Payment Integration</p>
</div>
""", unsafe_allow_html=True)

# Initialize session state
init_session_state()

# --------- Print Manager Page ----------
def render_print_manager_page():
    """Enhanced Print Manager with real-time updates"""
    
    # Auto-refresh for active jobs
    if AUTORELOAD_AVAILABLE and (st.session_state.get("current_job_id") or st.session_state.get("waiting_for_payment")):
        st_autorefresh(interval=3000, key="auto_refresh_sender", limit=1000)
    else:
        # Fallback refresh using JavaScript
        if st.session_state.get("current_job_id") or st.session_state.get("waiting_for_payment"):
            js = """
            <script>
                function autoRefresh() {
                    if (document.visibilityState === 'visible') {
                        setTimeout(() => {
                            window.location.reload();
                        }, 4000);
                    }
                }
                if (document.readyState === 'complete') {
                    autoRefresh();
                } else {
                    document.addEventListener('DOMContentLoaded', autoRefresh);
                }
            </script>
            """
            components.html(js, height=0)

    # Process real-time updates
    if process_ack_queue():
        st.rerun()

    st.header("📄 Print Service Dashboard")
    
    # Status display
    if st.session_state.get("status"):
        if st.session_state.get("waiting_for_payment"):
            st.markdown(f"""
            <div class="status-info">
                <strong>🕐 Status:</strong> {st.session_state['status']} - Waiting for payment confirmation...
            </div>
            """, unsafe_allow_html=True)
        else:
            st.info(f"📊 **Status:** {st.session_state['status']}")

    # User information section
    with st.expander("👤 User Information", expanded=True):
        col1, col2 = st.columns([2, 1])
        with col1:
            user_name = st.text_input(
                "Your name (optional)", 
                value=st.session_state.get("user_name", ""), 
                placeholder="Enter your name for print identification", 
                help="This helps identify your print job at the printer"
            )
            if user_name != st.session_state.get("user_name", ""):
                st.session_state["user_name"] = user_name
        
        with col2:
            st.markdown(f"**🆔 Session ID:**")
            st.code(st.session_state['user_id'], language=None)

    # File upload section
    st.markdown("### 📁 Upload Files")
    
    uploaded = st.file_uploader(
        "Select files to print", 
        accept_multiple_files=True, 
        type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'], 
        key="pm_multi_upload",
        help="Supported formats: PDF, Word, PowerPoint, Images, Text files"
    )
    
    if uploaded:
        with st.spinner("🔄 Converting files to PDF..."):
            conv_list = st.session_state.get("converted_files_pm", [])
            new_files = 0
            failed_files = []
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            for idx, uf in enumerate(uploaded):
                status_text.text(f"Processing {idx+1}/{len(uploaded)}: {uf.name}")
                progress_bar.progress(int((idx / len(uploaded)) * 100))
                
                # Skip if already processed
                if any(x.orig_name == uf.name for x in conv_list):
                    continue
                
                try:
                    log(f"Converting uploaded file: {uf.name}", "info")
                    original_bytes = uf.getvalue()
                    
                    if len(original_bytes) == 0:
                        failed_files.append((uf.name, "Empty file"))
                        continue
                    
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                    
                    if pdf_bytes and len(pdf_bytes) > 0:
                        cf = ConvertedFile(
                            orig_name=uf.name, 
                            pdf_name=os.path.splitext(uf.name)[0] + ".pdf", 
                            pdf_bytes=pdf_bytes, 
                            settings=PrintSettings(), 
                            original_bytes=original_bytes
                        )
                        log(f"Successfully converted: {uf.name} -> {len(pdf_bytes)} bytes", "debug")
                    else:
                        # If PDF conversion fails, store original for manual handling
                        cf = ConvertedFile(
                            orig_name=uf.name, 
                            pdf_name=uf.name, 
                            pdf_bytes=b"", 
                            settings=PrintSettings(), 
                            original_bytes=original_bytes
                        )
                        log(f"PDF conversion failed for {uf.name}, stored original", "warning")
                    
                    conv_list.append(cf)
                    new_files += 1
                    
                except Exception as e:
                    log(f"File processing failed for {uf.name}: {e}", "error")
                    failed_files.append((uf.name, str(e)))
            
            progress_bar.progress(100)
            status_text.empty()
            progress_bar.empty()
            
            if new_files > 0:
                st.session_state.converted_files_pm = conv_list
                st.success(f"✅ Successfully processed {new_files} file(s)")
            
            if failed_files:
                st.warning(f"⚠️ Failed to process {len(failed_files)} files:")
                for fname, error in failed_files:
                    st.write(f"• **{fname}**: {error}")

    # File queue display
    st.markdown("### 📋 Print Queue")
    conv = st.session_state.get("converted_files_pm", [])
    
    if not conv:
        st.info("📝 No files in queue. Upload files above to get started.")
    else:
        # Summary metrics
        total_files = len(conv)
        total_pages = sum(count_pdf_pages(cf.pdf_bytes or cf.original_bytes or b'') for cf in conv)
        total_size = sum(len(cf.pdf_bytes or cf.original_bytes or b'') for cf in conv)
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(f"""
            <div class="metric-card">
                <h3 style="color: #667eea; margin: 0;">📁 {total_files}</h3>
                <p style="margin: 5px 0 0 0; color: #666;">Files</p>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            st.markdown(f"""
            <div class="metric-card">
                <h3 style="color: #667eea; margin: 0;">📑 {total_pages}</h3>
                <p style="margin: 5px 0 0 0; color: #666;">Total Pages</p>
            </div>
            """, unsafe_allow_html=True)
        with col3:
            st.markdown(f"""
            <div class="metric-card">
                <h3 style="color: #667eea; margin: 0;">💾 {total_size/1024/1024:.1f} MB</h3>
                <p style="margin: 5px 0 0 0; color: #666;">Total Size</p>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown("---")
        
        # File list with enhanced UI
        for idx, cf in enumerate(conv):
            with st.container():
                # File header
                col1, col2 = st.columns([4, 1])
                with col1:
                    checked_key = f"sel_file_{idx}"
                    if checked_key not in st.session_state:
                        st.session_state[checked_key] = True
                    
                    is_selected = st.checkbox(
                        f"📄 **{cf.pdf_name}**", 
                        value=st.session_state[checked_key], 
                        key=checked_key,
                        help=f"Original: {cf.orig_name}"
                    )
                
                with col2:
                    blob_for_count = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b'')
                    pages = count_pdf_pages(blob_for_count)
                    st.metric("Pages", pages)
                
                # File actions
                if is_selected:
                    col1, col2, col3, col4 = st.columns(4)
                    
                    with col1:
                        if st.button("👁️ Preview", key=f"preview_pm_{idx}"):
                            if cf.pdf_bytes:
                                b64 = base64.b64encode(cf.pdf_bytes).decode('utf-8')
                                ts = int(time.time()*1000)
                                js = f"""
                                <script>
                                (function(){{
                                    try {{
                                        const b64="{b64}";
                                        const bytes=atob(b64);
                                        const arr=new Uint8Array(bytes.length);
                                        for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                                        const blob=new Blob([arr],{{type:'application/pdf'}});
                                        const url=URL.createObjectURL(blob);
                                        const w=window.open(url,'preview_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes');
                                        if(!w) alert('Please allow popups to preview files.');
                                    }} catch(e) {{
                                        alert('Preview error: ' + e.message);
                                    }}
                                }})();
                                </script>
                                """
                                components.html(js, height=0)
                            else:
                                st.warning("⚠️ PDF preview not available for this file")
                    
                    with col2:
                        if cf.pdf_bytes:
                            st.download_button(
                                "📥 PDF", 
                                data=cf.pdf_bytes, 
                                file_name=cf.pdf_name, 
                                mime="application/pdf", 
                                key=f"dl_pdf_{idx}"
                            )
                        else:
                            st.download_button(
                                "📥 Original", 
                                data=cf.original_bytes or b"", 
                                file_name=cf.orig_name, 
                                key=f"dl_orig_{idx}"
                            )
                    
                    with col3:
                        if st.button("🗑️ Remove", key=f"rm_{idx}", help="Remove from queue"):
                            st.session_state.converted_files_pm = [x for x in conv if x.orig_name != cf.orig_name]
                            st.rerun()
                    
                    with col4:
                        file_size = len(cf.pdf_bytes or cf.original_bytes or b'')
                        st.caption(f"Size: {file_size/1024:.1f} KB")
                
                st.markdown("---")

        # Print settings and submission
        selected_files = [cf for idx, cf in enumerate(conv) if st.session_state.get(f"sel_file_{idx}", True)]
        
        if selected_files:
            st.markdown("### ⚙️ Print Settings")
            
            col1, col2, col3 = st.columns(3)
            with col1:
                copies = st.number_input(
                    "📑 Copies per file", 
                    min_value=1, 
                    max_value=10, 
                    value=1, 
                    key="pm_job_copies",
                    help="Number of copies for each file"
                )
            with col2:
                color_mode = st.selectbox(
                    "🎨 Print Mode", 
                    options=["Auto", "Color", "Monochrome"], 
                    key="pm_job_colormode",
                    help="Color preference for printing"
                )
            with col3:
                quality = st.selectbox(
                    "⚙️ Quality", 
                    options=["High", "Standard", "Draft"], 
                    key="pm_job_quality",
                    help="Print quality setting"
                )
            
            # Calculate totals
            total_pages = sum(count_pdf_pages(cf.pdf_bytes or cf.original_bytes or b'') for cf in selected_files)
            total_copies = total_pages * copies
            
            # Summary before submission
            st.markdown("#### 📊 Print Summary")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("📁 Files", len(selected_files))
            col2.metric("📑 Pages", total_pages)
            col3.metric("📇 Total Copies", total_copies)
            col4.metric("💰 Est. Cost", f"₹{total_copies * 2:.0f}")  # Example pricing
            
            # Submit button
            if st.button("🖨️ **Send Print Job**", type="primary", use_container_width=True, key="pm_send_multi"):
                if not selected_files:
                    st.error("Please select at least one file to print.")
                else:
                    send_multiple_files_firestore(selected_files, copies, color_mode)

    # Process ACK queue updates
    process_ack_queue()

    # Print acknowledgment display
    print_ack = st.session_state.get("print_ack")
    if print_ack:
        st.markdown("### 🖨️ Print Status")
        status = print_ack.get('status', 'unknown') if isinstance(print_ack, dict) else str(print_ack)
        note = print_ack.get('note', '') if isinstance(print_ack, dict) else ''
        
        if status.lower() in ['completed', 'success', 'printed']:
            st.success(f"🎉 **Print completed successfully!** {note}")
        elif status.lower() in ['error', 'failed']:
            st.error(f"❌ **Print failed:** {note}")
        else:
            st.info(f"📄 **Print status:** {status} - {note}")

    # Enhanced Payment UI - Main Feature
    payinfo = st.session_state.get("payinfo")
    if payinfo and not st.session_state.get("process_complete"):
        show_enhanced_payment_ui(payinfo)

    # Process completion display
    if st.session_state.get("process_complete"):
        st.markdown("---")
        st.markdown("""
        <div class="success-card">
            <h2>🎉 Process Complete!</h2>
            <p style="font-size: 18px; margin: 15px 0;">Thank you for using our print service</p>
            <p style="opacity: 0.9;">Your documents have been sent for printing</p>
        </div>
        """, unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns([1,2,1])
        with col2:
            if st.button("🔄 **Start New Print Job**", type="primary", use_container_width=True, key="new_job_btn"):
                # Reset session for new job
                keys_to_reset = [
                    "process_complete", "payinfo", "status", "print_ack", 
                    "current_job_id", "current_file_ids", "waiting_for_payment", 
                    "converted_files_pm"
                ]
                for key in keys_to_reset:
                    st.session_state[key] = [] if key == "converted_files_pm" else None
                
                st.session_state["user_id"] = str(uuid.uuid4())[:8]
                set_status("Ready for new print job")
                st.rerun()

# --------- Convert & Format Page ----------
def render_convert_page():
    """Enhanced document conversion page"""
    st.header("🔄 Convert & Format Documents")
    st.write("Batch convert documents to PDF format. Preview, download, or add to print queue.")

    # File upload for conversion
    uploaded = st.file_uploader(
        "📁 Upload files to convert", 
        accept_multiple_files=True,
        type=['txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx','pdf'],
        key="conv_upload",
        help="Convert various file formats to PDF for consistent printing"
    )
    
    if uploaded:
        with st.spinner("🔄 Converting files..."):
            converted = []
            failed = []
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            for idx, uf in enumerate(uploaded):
                status_text.text(f"Converting {idx+1}/{len(uploaded)}: {uf.name}")
                progress_bar.progress(int((idx / len(uploaded)) * 100))
                
                try:
                    log(f"Converting file for format page: {uf.name}", "info")
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                    
                    if pdf_bytes:
                        converted.append({
                            "orig_name": uf.name,
                            "pdf_name": os.path.splitext(uf.name)[0] + ".pdf",
                            "pdf_bytes": pdf_bytes,
                            "pdf_base64": base64.b64encode(pdf_bytes).decode('utf-8'),
                            "pages": count_pdf_pages(pdf_bytes),
                            "size_mb": len(pdf_bytes) / (1024*1024)
                        })
                        log(f"Conversion successful: {uf.name}", "debug")
                    else:
                        failed.append(uf.name)
                        log(f"Conversion failed: {uf.name}", "warning")
                        
                except Exception as e:
                    failed.append(uf.name)
                    log(f"Conversion error for {uf.name}: {e}", "error")
            
            progress_bar.progress(100)
            status_text.empty()
            progress_bar.empty()
            
            if converted:
                st.session_state.converted_files_conv = converted
                st.success(f"✅ Successfully converted {len(converted)} files")
            
            if failed:
                st.error(f"❌ Failed to convert {len(failed)} files:")
                for fname in failed:
                    st.write(f"• {fname}")

    # Display converted files
    converted_files = st.session_state.get("converted_files_conv", [])
    if converted_files:
        st.markdown("### 📄 Converted Files")
        
        # Summary statistics
        total_files = len(converted_files)
        total_pages = sum(item.get("pages", 1) for item in converted_files)
        total_size = sum(item.get("size_mb", 0) for item in converted_files)
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(f"""
            <div class="metric-card">
                <h3 style="color: #28a745; margin: 0;">📁 {total_files}</h3>
                <p style="margin: 5px 0 0 0;">Files</p>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            st.markdown(f"""
            <div class="metric-card">
                <h3 style="color: #28a745; margin: 0;">📑 {total_pages}</h3>
                <p style="margin: 5px 0 0 0;">Total Pages</p>
            </div>
            """, unsafe_allow_html=True)
        with col3:
            st.markdown(f"""
            <div class="metric-card">
                <h3 style="color: #28a745; margin: 0;">💾 {total_size:.1f} MB</h3>
                <p style="margin: 5px 0 0 0;">Total Size</p>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown("---")
        
        # File list with enhanced actions
        for i, item in enumerate(converted_files):
            with st.container():
                col1, col2, col3, col4, col5, col6 = st.columns([3, 1, 1, 1, 1, 1])
                
                with col1:
                    st.markdown(f"**📄 {item['pdf_name']}**")
                    st.caption(f"Original: {item['orig_name']} • {item.get('pages', 1)} pages • {item.get('size_mb', 0):.1f} MB")
                
                with col2:
                    if st.button("👁️ Preview", key=f"c_preview_{i}"):
                        b64 = item['pdf_base64']
                        ts = int(time.time()*1000)
                        js = f"""
                        <script>
                        (function(){{
                            try {{
                                const b64="{b64}";
                                const bytes=atob(b64);
                                const arr=new Uint8Array(bytes.length);
                                for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                                const blob=new Blob([arr],{{type:'application/pdf'}});
                                const url=URL.createObjectURL(blob);
                                const w=window.open(url,'conv_preview_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes');
                                if(!w) alert('Please allow popups to preview files.');
                            }} catch(e) {{
                                alert('Preview error: ' + e.message);
                            }}
                        }})();
                        </script>
                        """
                        components.html(js, height=0)
                
                with col3:
                    st.download_button(
                        "📥 Download", 
                        data=item['pdf_bytes'], 
                        file_name=item['pdf_name'], 
                        mime="application/pdf",
                        key=f"c_download_{i}"
                    )
                
                with col4:
                    if st.button("🖨️ Print", key=f"c_print_{i}"):
                        b64 = item['pdf_base64']
                        ts = int(time.time()*1000)
                        js = f"""
                        <script>
                        (function(){{
                            try {{
                                const b64="{b64}";
                                const bytes=atob(b64);
                                const arr=new Uint8Array(bytes.length);
                                for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                                const blob=new Blob([arr],{{type:'application/pdf'}});
                                const url=URL.createObjectURL(blob);
                                const pop = window.open(url,'conv_print_{ts}','width=900,height=700');
                                if(pop){{ 
                                    setTimeout(()=>{{ 
                                        try{{ pop.print(); }}catch(e){{}} 
                                    }}, 1000); 
                                }} else {{ 
                                    alert('Please allow popups for printing.'); 
                                }}
                            }} catch(e) {{
                                alert('Print error: ' + e.message);
                            }}
                        }})();
                        </script>
                        """
                        components.html(js, height=0)
                
                with col5:
                    if st.button("➕ Add", key=f"c_add_{i}", help="Add to print queue"):
                        # Add to print manager queue
                        pm_files = st.session_state.get("converted_files_pm", [])
                        
                        # Check if already exists
                        if not any(x.orig_name == item['orig_name'] for x in pm_files):
                            cf = ConvertedFile(
                                orig_name=item['orig_name'],
                                pdf_name=item['pdf_name'],
                                pdf_bytes=item['pdf_bytes'],
                                settings=PrintSettings(),
                                original_bytes=None
                            )
                            pm_files.append(cf)
                            st.session_state.converted_files_pm = pm_files
                            st.success(f"✅ Added {item['pdf_name']} to print queue")
                        else:
                            st.info("ℹ️ File already in print queue")
                
                with col6:
                    if st.button("🗑️", key=f"c_remove_{i}", help="Remove file"):
                        st.session_state.converted_files_conv.pop(i)
                        st.rerun()
                
                st.markdown("---")
        
        # Bulk actions
        if converted_files:
            st.markdown("### 🔧 Bulk Actions")
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if st.button("➕ **Add All to Print Queue**", use_container_width=True, key="bulk_add_print"):
                    pm_files = st.session_state.get("converted_files_pm", [])
                    added = 0
                    
                    for item in converted_files:
                        if not any(x.orig_name == item['orig_name'] for x in pm_files):
                            cf = ConvertedFile(
                                orig_name=item['orig_name'],
                                pdf_name=item['pdf_name'],
                                pdf_bytes=item['pdf_bytes'],
                                settings=PrintSettings(),
                                original_bytes=None
                            )
                            pm_files.append(cf)
                            added += 1
                    
                    st.session_state.converted_files_pm = pm_files
                    if added > 0:
                        st.success(f"✅ Added {added} files to print queue")
                    else:
                        st.info("ℹ️ All files already in print queue")
            
            with col2:
                if st.button("📥 **Download All**", use_container_width=True, key="bulk_download"):
                    st.info("💡 Individual downloads available above. ZIP download coming soon!")
            
            with col3:
                if st.button("🗑️ **Clear All**", use_container_width=True, key="bulk_clear"):
                    st.session_state.converted_files_conv = []
                    st.success("🗑️ Cleared all converted files")
                    st.rerun()

# --------- Main Application ----------
def main():
    """Main application with enhanced navigation"""
    
    # Sidebar navigation
    with st.sidebar:
        st.markdown("## 🧭 Navigation")
        page = st.radio(
            "Select Page:", 
            ["🖨️ Print Manager", "🔄 Convert & Format"], 
            index=0,
            help="Choose between printing files or converting documents"
        )
        
        st.markdown("---")
        
        # Current session info
        st.markdown("## 📊 Session Info")
        if st.session_state.get("user_name"):
            st.markdown(f"**👤 User:** {st.session_state['user_name']}")
        st.markdown(f"**🆔 Session:** `{st.session_state.get('user_id', 'unknown')}`")
        
        # Current job status
        if st.session_state.get("current_job_id"):
            st.markdown(f"**📋 Active Job:** `{st.session_state['current_job_id'][:8]}...`")
            
            if st.session_state.get("waiting_for_payment"):
                st.markdown("**💳 Status:** Waiting for payment")
            elif st.session_state.get("process_complete"):
                st.markdown("**✅ Status:** Complete")
            else:
                st.markdown("**🔄 Status:** Processing")
        
        # File counts
        pm_files = len(st.session_state.get("converted_files_pm", []))
        conv_files = len(st.session_state.get("converted_files_conv", []))
        
        if pm_files > 0:
            st.markdown(f"**📁 Print Queue:** {pm_files} files")
        if conv_files > 0:
            st.markdown(f"**🔄 Converted:** {conv_files} files")
        
        st.markdown("---")
        
        # Help and info
        st.markdown("## ℹ️ About")
        st.markdown("""
        **Autoprint Service** provides:
        - 📄 Document conversion to PDF
        - 🖨️ Professional printing services  
        - 💳 Secure UPI payment integration
        - 📱 Real-time status updates
        - 🔄 Multi-format file support
        
        **Supported Formats:**
        - PDF, Word, PowerPoint
        - Images (JPG, PNG, etc.)
        - Text files (TXT, MD, HTML)
        """)
        
        # Debug info (only show if there are issues)
        if st.session_state.get("status") and "failed" in st.session_state["status"].lower():
            with st.expander("🔧 Debug Info"):
                st.write(f"Status: {st.session_state.get('status', 'N/A')}")
                st.write(f"Job ID: {st.session_state.get('current_job_id', 'N/A')}")
                st.write(f"File IDs: {len(st.session_state.get('current_file_ids', []))}")
                if st.button("View Logs"):
                    st.text(f"Log file: {LOGFILE}")
        
        st.markdown("---")
        
        # Emergency reset
        if st.button("🔄 **Reset Session**", help="Clear all data and start fresh"):
            # Clean up listeners first
            detach_job_listener()
            detach_file_listeners()
            
            # Clear all session state
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            
            log("Session reset by user", "info")
            st.rerun()

    # Main content area
    if "Print Manager" in page:
        render_print_manager_page()
    else:
        render_convert_page()

    # Footer with enhanced styling
    st.markdown("---")
    st.markdown("""
    <div style="text-align: center; padding: 20px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); border-radius: 10px; margin: 20px 0;">
        <h4 style="color: #495057; margin: 0;">🖨️ <strong>Autoprint Service</strong></h4>
        <p style="color: #6c757d; margin: 5px 0 0 0;">Professional Document Printing with UPI Integration</p>
        <p style="color: #6c757d; margin: 5px 0 0 0; font-size: 14px;">
            📧 Support: <a href="mailto:support@autoprint.service">support@autoprint.service</a> | 
            📞 Help: +91-XXXX-XXXXXX
        </p>
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.error(f"❌ Application error: {e}")
        log(f"Application error: {e}", "error")
        logger.debug(traceback.format_exc())
        
        # Show debug information
        with st.expander("🔧 Error Details"):
            st.text(f"Error: {e}")
            st.text(f"Log file: {LOGFILE}")
            if st.button("Reset Application"):
                st.session_state.clear()
                st.rerun()
