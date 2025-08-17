analyze the following code step by step 

# wo3_autoprint_streamlit_firestore_sender_complete_fixed_docx.py
# Streamlit sender that uploads chunked base64 docs + manifest to Firestore
# Includes robust conversion with docx XML fallback
#
# Run: streamlit run wo3_autoprint_streamlit_firestore_sender_complete_fixed_docx.py

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
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from fpdf import FPDF
from PIL import Image
from pathlib import Path
import hashlib
import datetime
import uuid
import webbrowser
import io
import threading
import zipfile
import xml.etree.ElementTree as ET

# Firestore
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

# Optional PDF page counter
try:
    from PyPDF2 import PdfReader
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

# optional imports (soft)
try:
    import pypandoc
    PYPANDOC_AVAILABLE = True
except Exception:
    PYPANDOC_AVAILABLE = False

try:
    import docx2pdf
    DOCX2PDF_AVAILABLE = True
except Exception:
    DOCX2PDF_AVAILABLE = False

# python-docx (for docx text fallback)
try:
    import docx as python_docx
    PYTHON_DOCX_AVAILABLE = True
except Exception:
    python_docx = None
    PYTHON_DOCX_AVAILABLE = False

# python-pptx (for pptx text fallback)
try:
    import pptx as python_pptx
    PYTHON_PPTX_AVAILABLE = True
except Exception:
    python_pptx = None
    PYTHON_PPTX_AVAILABLE = False

try:
    from spire.presentation import Presentation, FileFormat
    SPIRE_AVAILABLE = True
except Exception:
    SPIRE_AVAILABLE = False

# --------- Logging ----------
LOGFILE = os.path.join(tempfile.gettempdir(), f"autoprint_sender_{int(time.time())}.log")
logger = logging.getLogger("autoprint_sender")
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

# --------- Utilities ----------
def safe_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception as e:
        log(f"safe_remove({path}) failed: {e}", "warning")

def find_executable(names):
    for name in names:
        if os.path.exists(name):
            return name
        path = shutil.which(name)
        if path:
            return path
    return None

def run_subprocess(cmd: List[str], timeout: int = 60):
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return True, out
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "") + (e.stderr or "")
        out += f"\nexit:{e.returncode}"
        return False, out
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        out += f"\nTimeout after {timeout}s"
        return False, out
    except FileNotFoundError as e:
        return False, str(e)
    except Exception as e:
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
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            name = getattr(func, "__name__", str(func))
            log(f"Attempt {i+1}/{attempts} failed for {name}: {e}", "warning")
            logger.debug(traceback.format_exc())
            time.sleep(delay)
            delay *= factor
    log(f"All {attempts} attempts failed for {getattr(func, '__name__', str(func))}", "error")
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
    original_bytes: Optional[bytes] = None  # saved original upload bytes for fallback

# --------- FileConverter (robust with docx-xml fallback) ----------
class FileConverter:
    SUPPORTED_TEXT_EXTENSIONS = {'.txt', '.md', '.rtf', '.html', '.htm'}
    SUPPORTED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp'}
    LIBREOFFICE_TIMEOUT = 60
    PANDOC_TIMEOUT = 50

    @staticmethod
    def _fpdf_from_lines(lines: List[str]) -> bytes:
        pdf = FPDF(unit='mm', format='A4')
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        for line in lines:
            try:
                pdf.multi_cell(0, 6, line)
            except Exception:
                pdf.cell(0, 6, txt=line[:200], ln=1)
        return pdf.output(dest='S').encode('latin-1', errors='replace')

    @classmethod
    def convert_text_to_pdf_bytes(cls, file_content: bytes, encoding='utf-8') -> Optional[bytes]:
        try:
            text = file_content.decode(encoding, errors='ignore')
            lines = []
            for paragraph in text.splitlines():
                if paragraph.strip() == "":
                    lines.append("")
                    continue
                maxlen = 200
                while len(paragraph) > maxlen:
                    lines.append(paragraph[:maxlen])
                    paragraph = paragraph[maxlen:]
                lines.append(paragraph)
            return cls._fpdf_from_lines(lines)
        except Exception as e:
            log(f"convert_text_to_pdf_bytes failed: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def convert_image_to_pdf_bytes(cls, file_content: bytes) -> Optional[bytes]:
        try:
            from io import BytesIO
            with Image.open(BytesIO(file_content)) as img:
                try:
                    resample_filter = Image.Resampling.LANCZOS
                except Exception:
                    try:
                        resample_filter = Image.LANCZOS
                    except Exception:
                        resample_filter = Image.BICUBIC
                max_dim = 2000
                if img.width > max_dim or img.height > max_dim:
                    img.thumbnail((max_dim, max_dim), resample=resample_filter)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                out = BytesIO()
                img.save(out, format='PDF')
                data = out.getvalue()
                out.close()
                return data
        except Exception as e:
            log(f"convert_image_to_pdf_bytes failed: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def _convert_docx_with_python_docx(cls, input_path: str) -> Optional[bytes]:
        try:
            if not PYTHON_DOCX_AVAILABLE:
                return None
            doc = python_docx.Document(input_path)
            paragraphs = [p.text for p in doc.paragraphs]
            text = "\n".join(paragraphs)
            return cls.convert_text_to_pdf_bytes(text.encode('utf-8'))
        except Exception:
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def _extract_text_from_docx_xml(cls, input_path: str) -> Optional[str]:
        # Open the .docx (zip) and extract word/document.xml, then extract w:t nodes
        try:
            with zipfile.ZipFile(input_path, 'r') as z:
                if 'word/document.xml' not in z.namelist():
                    return None
                raw = z.read('word/document.xml')
            # Parse XML and extract text nodes
            root = ET.fromstring(raw)
            # common namespace for WordprocessingML
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            texts = []
            for t in root.iter():
                # Element tag endswith 't' (text) usually in the w namespace
                if t.tag.endswith('}t') or t.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t':
                    if t.text:
                        texts.append(t.text)
            if texts:
                return "\n".join(texts)
            return None
        except Exception:
            logger.debug("docx xml extract failed:\n" + traceback.format_exc())
            return None

    @classmethod
    def convert_docx_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        input_path = os.path.abspath(input_path)
        out_pdf = os.path.join(tempfile.gettempdir(), f"docx_out_{int(time.time()*1000)}.pdf")
        headless = system_is_headless()

        # Try docx2pdf if interactive environment and module available
        if not headless and DOCX2PDF_AVAILABLE:
            try:
                def _try_docx2pdf():
                    try:
                        docx2pdf.convert(input_path, os.path.dirname(out_pdf))
                    except TypeError:
                        docx2pdf.convert(input_path, out_pdf)
                    expected = os.path.join(os.path.dirname(out_pdf),
                                            os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                    if os.path.exists(expected) and expected != out_pdf:
                        os.replace(expected, out_pdf)
                    return os.path.exists(out_pdf)
                ok = retry_with_backoff(_try_docx2pdf, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception:
                logger.debug(traceback.format_exc())

        # Try LibreOffice headless
        soffice = find_executable([
            "soffice", "libreoffice",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            "/usr/bin/libreoffice"
        ])
        if soffice:
            try:
                def _try_libre():
                    cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(out_pdf), input_path]
                    ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                    expected = os.path.join(os.path.dirname(out_pdf),
                                            os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                    if os.path.exists(expected):
                        if expected != out_pdf:
                            os.replace(expected, out_pdf)
                        return os.path.exists(out_pdf)
                    return False
                ok = retry_with_backoff(_try_libre, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception:
                logger.debug(traceback.format_exc())

        # Fallback 1: python-docx text extraction
        txt_fallback = cls._convert_docx_with_python_docx(input_path)
        if txt_fallback:
            return txt_fallback

        # Fallback 2: Parse document.xml from the .docx zip (works without extra libs)
        xml_text = cls._extract_text_from_docx_xml(input_path)
        if xml_text:
            pdf_bytes = cls.convert_text_to_pdf_bytes(xml_text.encode('utf-8'))
            if pdf_bytes:
                return pdf_bytes

        # Final fallback: create a simple PDF saying conversion not available and include filename
        try:
            msg = f"Conversion not available for file: {os.path.basename(input_path)}\n\nPlease download original file or try converting on a machine with LibreOffice/docx2pdf."
            return cls.convert_text_to_pdf_bytes(msg.encode('utf-8'))
        except Exception:
            logger.debug(traceback.format_exc())
            safe_remove(out_pdf)
            return None

    @classmethod
    def _convert_pptx_with_python_pptx(cls, input_path: str) -> Optional[bytes]:
        try:
            if not PYTHON_PPTX_AVAILABLE:
                return None
            prs = python_pptx.Presentation(input_path)
            pages = []
            for slide in prs.slides:
                slide_text_parts = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        slide_text_parts.append(shape.text)
                pages.append("\n".join(slide_text_parts) or "[No textual content on slide]")
            pdf = FPDF(unit='mm', format='A4')
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.set_font("Helvetica", size=12)
            for p in pages:
                pdf.add_page()
                pdf.multi_cell(0, 6, p)
            return pdf.output(dest='S').encode('latin-1', errors='replace')
        except Exception:
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def convert_pptx_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        input_path = os.path.abspath(input_path)
        out_pdf = os.path.join(tempfile.gettempdir(), f"pptx_out_{int(time.time()*1000)}.pdf")

        if SPIRE_AVAILABLE:
            try:
                pres = Presentation()
                pres.LoadFromFile(input_path)
                pres.SaveToFile(out_pdf, FileFormat.PDF)
                pres.Dispose()
                if os.path.exists(out_pdf):
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception:
                logger.debug(traceback.format_exc())

        soffice = find_executable(["soffice", "libreoffice", "/usr/bin/libreoffice"])
        if soffice:
            try:
                def _try_libre():
                    cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(out_pdf), input_path]
                    ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                    expected = os.path.join(os.path.dirname(out_pdf),
                                            os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                    if os.path.exists(expected):
                        if expected != out_pdf:
                            os.replace(expected, out_pdf)
                        return os.path.exists(out_pdf)
                    return False
                ok = retry_with_backoff(_try_libre, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception:
                logger.debug(traceback.format_exc())

        pptx_fallback = cls._convert_pptx_with_python_pptx(input_path)
        if pptx_fallback:
            return pptx_fallback

        safe_remove(out_pdf)
        return None

    @classmethod
    def convert_generic_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        out_pdf = os.path.join(tempfile.gettempdir(), f"generic_out_{int(time.time()*1000)}.pdf")
        soffice = find_executable(["soffice", "libreoffice", "/usr/bin/libreoffice"])
        if soffice:
            try:
                cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(out_pdf), input_path]
                ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                expected = os.path.join(os.path.dirname(out_pdf),
                                        os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                if os.path.exists(expected):
                    if expected != out_pdf:
                        os.replace(expected, out_pdf)
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception:
                logger.debug(traceback.format_exc())
        if PYPANDOC_AVAILABLE:
            try:
                pandoc_exec = find_executable(["pandoc"])
                if pandoc_exec:
                    cmd = [pandoc_exec, input_path, "-o", out_pdf]
                    ok, out = run_subprocess(cmd, timeout=cls.PANDOC_TIMEOUT)
                    if ok and os.path.exists(out_pdf):
                        with open(out_pdf, "rb") as f:
                            data = f.read()
                        safe_remove(out_pdf)
                        return data
            except Exception:
                logger.debug(traceback.format_exc())
        safe_remove(out_pdf)
        return None

    @classmethod
    def convert_uploaded_file_to_pdf_bytes(cls, uploaded_file) -> Optional[bytes]:
        if not uploaded_file:
            return None
        suffix = os.path.splitext(uploaded_file.name)[1].lower()
        content = uploaded_file.getvalue()
        try:
            if suffix == ".pdf":
                return content
            if suffix in cls.SUPPORTED_TEXT_EXTENSIONS:
                return cls.convert_text_to_pdf_bytes(content)
            if suffix in cls.SUPPORTED_IMAGE_EXTENSIONS:
                img_pdf = cls.convert_image_to_pdf_bytes(content)
                if img_pdf:
                    return img_pdf
            if suffix == ".docx":
                with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tf:
                    tf.write(content)
                    tf.flush()
                    tmpname = tf.name
                try:
                    res = cls.convert_docx_to_pdf_bytes(tmpname)
                    if res:
                        return res
                    # If conversion returned None, we still return None and the caller will warn and use original bytes as fallback.
                finally:
                    safe_remove(tmpname)
                return None
            if suffix == ".pptx":
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pptx") as tf:
                    tf.write(content)
                    tf.flush()
                    tmpname = tf.name
                try:
                    res = cls.convert_pptx_to_pdf_bytes(tmpname)
                    if res:
                        return res
                finally:
                    safe_remove(tmpname)
                return None
            # generic fallback: write file and try libreoffice/pandoc
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                tf.write(content)
                tf.flush()
                tmpname = tf.name
            try:
                res = cls.convert_generic_to_pdf_bytes(tmpname)
                return res
            finally:
                safe_remove(tmpname)
        except Exception as e:
            log(f"convert_uploaded_file_to_pdf_bytes failed for {uploaded_file.name}: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

# --------- Page counting ----------
def count_pdf_pages(blob: Optional[bytes]) -> int:
    if not blob:
        return 1
    if not PDF_READER_AVAILABLE:
        return 1
    try:
        stream = io.BytesIO(blob)
        reader = PdfReader(stream)
        return len(reader.pages)
    except Exception:
        logger.debug("count_pdf_pages failed:\n" + traceback.format_exc())
        return 1

# --------- Streamlit UI & Firestore uploader ----------
st.set_page_config(page_title="Autoprint (Firestore)", layout="wide", page_icon="🖨️")

st.markdown("""
<style>
  .appview-container .main .block-container {padding-top: 10px; padding-bottom:10px;}
  .stButton>button {padding:6px 10px;}
  .stDownloadButton>button {padding:6px 10px;}
  .stProgress {height:14px;}
</style>
""", unsafe_allow_html=True)

st.markdown("<h1 style='text-align:center;margin:6px 0 8px 0;'>Autoprint — Firestore Sender</h1>", unsafe_allow_html=True)

# Session state initial
if 'converted_files_pm' not in st.session_state:
    st.session_state.converted_files_pm = []
if 'payinfo' not in st.session_state:
    st.session_state.payinfo = None
if 'status' not in st.session_state:
    st.session_state.status = ""
if 'process_complete' not in st.session_state:
    st.session_state.process_complete = False
if 'user_name' not in st.session_state:
    st.session_state.user_name = ""
if 'user_id' not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())[:8]
if 'pricing' not in st.session_state:
    st.session_state.pricing = {
        "price_bw_per_page": 2.00,
        "price_color_per_page": 5.00,
        "price_duplex_discount": 0.9,
        "min_charge": 5.00,
        "currency": "INR",
        "owner_upi": "owner@upi"
    }

def set_status(s: str):
    st.session_state.status = f"{datetime.datetime.now().strftime('%H:%M:%S')} - {s}"

# Firestore init using st.secrets
COLLECTION = "files"
CHUNK_SIZE = 200_000  # characters per chunk when splitting base64

db = None
FIRESTORE_OK = False
FIRESTORE_ERR = None

def init_firestore_from_secrets():
    global db, FIRESTORE_OK, FIRESTORE_ERR
    try:
        svc = st.secrets.get("firebase_service_account") if hasattr(st, "secrets") else None
        if not svc:
            raise RuntimeError("Add 'firebase_service_account' to Streamlit Secrets (JSON string or map).")
        if isinstance(svc, dict):
            sa = svc
        else:
            sa = json.loads(svc)
        if "private_key" in sa and isinstance(sa["private_key"], str):
            sa["private_key"] = sa["private_key"].replace("\\n", "\n")
        if firebase_admin is None or credentials is None or firestore is None:
            raise RuntimeError("firebase_admin SDK not available in environment.")
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(sa)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        FIRESTORE_OK = True
        set_status("Firestore initialized")
        log("Firestore initialized", "info")
    except Exception as e:
        FIRESTORE_OK = False
        db = None
        FIRESTORE_ERR = str(e)
        set_status(f"Firestore init failed: {e}")
        log(f"Firestore init failed: {e}", "error")
        logger.debug(traceback.format_exc())

init_firestore_from_secrets()

if not FIRESTORE_OK:
    st.warning("Firestore not initialized. Add 'firebase_service_account' to Streamlit Secrets.")

# helpers
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def meta_doc_id(file_id: str) -> str:
    return f"{file_id}_meta"

def chunk_doc_id(file_id: str, idx: int) -> str:
    return f"{file_id}_{idx}"

# Pricing and upload functions (same as previous full file)
def calculate_amount(cfg, pages, copies=1, color=False, duplex=False):
    try:
        price_per_page = cfg.get("price_color_per_page") if color else cfg.get("price_bw_per_page")
        amt = pages * price_per_page * copies
        if duplex:
            amt *= cfg.get("price_duplex_discount", 1.0)
        if amt < cfg.get("min_charge", 0):
            amt = cfg.get("min_charge", 0)
        return round(float(amt), 2)
    except Exception:
        return float(cfg.get("min_charge", 0.0))

def generate_upi_uri(upi_id, amount, note=None):
    params = [f"pa={upi_id}", f"am={amount}"]
    if note:
        params.append(f"tn={note}")
    return "upi://pay?" + "&".join(params)

def send_multiple_files(converted_files: List[ConvertedFile], copies: int, color_mode: str):
    global db, FIRESTORE_OK
    if not FIRESTORE_OK or db is None:
        st.error("Firestore not initialized. Cannot upload.")
        return

    if not converted_files:
        st.error("No files to send.")
        return

    set_status("Preparing upload...")
    try:
        job_id = str(uuid.uuid4())[:12]
        files_meta = []
        total_bytes = 0

        # Prepare metadata and chunking
        for cf in converted_files:
            blob = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
            size = len(blob)
            pages = count_pdf_pages(blob)
            fid = str(uuid.uuid4())[:8]
            b64 = base64.b64encode(blob).decode("utf-8")
            parts = [b64[i:i+CHUNK_SIZE] for i in range(0, len(b64), CHUNK_SIZE)] if b64 else []
            sha = sha256_bytes(blob) if blob else ""
            files_meta.append({
                "file_id": fid,
                "filename": cf.pdf_name,
                "orig_filename": cf.orig_name,
                "size_bytes": size,
                "pages": pages,
                "settings": {
                    "copies": copies,
                    "duplex": cf.settings.duplex,
                    "colorMode": color_mode,
                    "paperSize": cf.settings.paper_size,
                    "orientation": cf.settings.orientation,
                    "collate": cf.settings.collate
                },
                "will_send_converted": bool(cf.pdf_bytes),
                "parts": parts,
                "total_chunks": len(parts),
                "sha256": sha
            })
            total_bytes += size

        # Upload chunks with progress bar
        set_status("Uploading chunks to Firestore...")
        progress_bar = st.progress(0.0)
        total_chunks_all = sum(m["total_chunks"] for m in files_meta)
        uploaded_chunks = 0

        for m in files_meta:
            fid = m["file_id"]
            for idx, piece in enumerate(m["parts"]):
                doc_ref = db.collection(COLLECTION).document(chunk_doc_id(fid, idx))
                def _write_chunk(dref=doc_ref, data_piece=piece, i=idx):
                    dref.set({"data": data_piece, "chunk_index": i})
                retry_with_backoff(_write_chunk, attempts=3)
                uploaded_chunks += 1
                try:
                    progress_bar.progress(min(1.0, uploaded_chunks / max(1, total_chunks_all)))
                except Exception:
                    pass

        set_status("All chunks uploaded. Writing manifests...")

        # Write manifest documents
        for m in files_meta:
            fid = m["file_id"]
            meta_doc = {
                "total_chunks": int(m["total_chunks"]),
                "file_name": m["filename"],
                "sha256": m["sha256"],
                "settings": m.get("settings", {}),
                "user_name": st.session_state.get("user_name") or "",
                "user_id": st.session_state.get("user_id"),
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "job_id": job_id,
                "file_size_bytes": int(m.get("size_bytes", 0)),
            }
            def _write_meta(mref=db.collection(COLLECTION).document(meta_doc_id(fid)), data=meta_doc):
                mref.set(data, merge=True)
            retry_with_backoff(_write_meta, attempts=3)
            set_status(f"Wrote manifest for {m['filename']} (id={fid})")

        # Poll for payinfo — show local estimate after short wait
        set_status("Waiting for receiver to write payinfo into manifest (polling)...")
        st.session_state.payinfo = None
        poll_start = time.time()
        short_wait = 6
        total_poll = 90
        local_estimate_shown = False

        while time.time() - poll_start < total_poll:
            found_payinfo = None
            for m in files_meta:
                fid = m["file_id"]
                try:
                    snap = db.collection(COLLECTION).document(meta_doc_id(fid)).get()
                    if snap.exists:
                        md = snap.to_dict() or {}
                        if md.get("payinfo"):
                            found_payinfo = md["payinfo"]
                            break
                except Exception:
                    logger.debug(traceback.format_exc())
            if found_payinfo:
                st.session_state.payinfo = found_payinfo
                set_status("Received payment info from receiver.")
                break
            # show local estimate after short_wait
            if not local_estimate_shown and (time.time() - poll_start) >= short_wait:
                cfg = st.session_state.get("pricing") or {}
                total_amount = 0.0
                for m in files_meta:
                    is_color = ("color" in str(m.get("settings", {}).get("colorMode", "")).lower()) or ("color" in str(color_mode).lower())
                    duplex_flag = False
                    d = m.get("settings", {}).get("duplex", "") or ""
                    if d and ("two" in str(d).lower() or "duplex" in str(d).lower()):
                        duplex_flag = True
                    amt = calculate_amount(cfg, m["pages"], copies=int(m["settings"].get("copies", 1)), color=is_color, duplex=duplex_flag)
                    total_amount += amt
                est = {
                    "order_id": job_id,
                    "file_name": "Multiple" if len(files_meta) > 1 else files_meta[0]["filename"],
                    "pages": sum(m["pages"] for m in files_meta),
                    "copies": copies,
                    "amount": round(total_amount, 2),
                    "amount_str": f"{round(total_amount, 2):.2f}",
                    "currency": cfg.get("currency", "INR"),
                    "owner_upi": cfg.get("owner_upi"),
                    "upi_url": f"upi://pay?pa={cfg.get('owner_upi')}&pn=PrintService&am={round(total_amount, 2):.2f}&cu={cfg.get('currency','INR')}",
                    "status": "estimated",
                    "estimated": True
                }
                st.session_state.payinfo = est
                local_estimate_shown = True
                set_status("Showing local estimate while waiting for official payinfo.")
            time.sleep(1.0)

        if not st.session_state.payinfo:
            set_status("No payinfo received; showing local estimate if available.")
        st.success(f"Upload complete. Job id: {job_id}")
        return

    except Exception as e:
        logger.debug(traceback.format_exc())
        st.error(f"Upload failed: {e}")
        set_status("Upload failed")
        return

def pay_offline():
    payinfo = st.session_state.get("payinfo") or {}
    amount = payinfo.get("amount", 0)
    currency = payinfo.get("currency", "INR")
    st.success(f"💵 Please pay ₹{amount} {currency} offline")
    st.balloons()
    st.session_state.payinfo = None
    st.session_state.process_complete = True

def pay_online():
    payinfo = st.session_state.get("payinfo") or {}
    owner_upi = payinfo.get("owner_upi")
    if not owner_upi:
        st.error("Payment information not available")
        return
    amount = payinfo.get("amount", 0)
    file_name = payinfo.get("file_name", "Print Job")
    upi_uri = generate_upi_uri(owner_upi, amount, note=f"Print: {file_name}")
    st.markdown(f"**💳 Pay ₹{amount} via UPI**")
    st.markdown(f"[🚀 **Open Payment App**]({upi_uri})")
    if QR_AVAILABLE:
        try:
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(upi_uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            st.image(img, width=200, caption="Scan with any UPI app")
        except Exception:
            pass
    try:
        webbrowser.open(upi_uri)
    except Exception:
        pass
    st.info("Complete the payment in your payment app.")
    st.balloons()
    st.session_state.payinfo = None
    st.session_state.process_complete = True

def cancel_payment():
    st.session_state.payinfo = None
    set_status("Cancelled by user")

# UI: Print Manager + Convert pages
st.sidebar.title("Autoprint Sender (Firestore)")
page = st.sidebar.radio("Page", ["Print Manager", "Convert & Format"])

with st.sidebar.expander("Environment"):
    st.write(f"Platform: {platform.system()}")
    st.write("PyPDF2 (page count):", PDF_READER_AVAILABLE)
    st.write("python-docx:", PYTHON_DOCX_AVAILABLE)
    st.write("python-pptx:", PYTHON_PPTX_AVAILABLE)
    st.write("docx2pdf:", DOCX2PDF_AVAILABLE)
    st.write("LibreOffice on PATH:", bool(find_executable(["soffice", "libreoffice"])))
    st.write("Log file:", LOGFILE)
    if st.button("Show log tail"):
        try:
            with open(LOGFILE, "r", encoding="utf-8") as lf:
                st.code(lf.read()[-4000:])
        except Exception as e:
            st.error(f"Could not read log file: {e}")

st.markdown("## 👤 User")
user_name = st.text_input("Your name (optional)", value=st.session_state.get("user_name", ""))
st.session_state.user_name = user_name
st.caption(f"Your ID: {st.session_state['user_id']}")

# conversion & queueing
if page == "Print Manager":
    st.header("📄 File Transfer & Print Service (Firestore chunked upload)")
    uploaded = st.file_uploader("Upload files (multiple)", accept_multiple_files=True,
                                type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'],
                                key="pm_multi_upload")
    if uploaded:
        with st.spinner("Converting uploads..."):
            conv_list = st.session_state.get("converted_files_pm", [])
            added = 0
            for uf in uploaded:
                if any(x.orig_name == uf.name for x in conv_list):
                    continue
                try:
                    original_bytes = uf.getvalue()
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                    if pdf_bytes:
                        cf = ConvertedFile(orig_name=uf.name,
                                           pdf_name=os.path.splitext(uf.name)[0] + ".pdf",
                                           pdf_bytes=pdf_bytes,
                                           settings=PrintSettings(),
                                           original_bytes=original_bytes)
                    else:
                        cf = ConvertedFile(orig_name=uf.name,
                                           pdf_name=uf.name,
                                           pdf_bytes=b"",
                                           settings=PrintSettings(),
                                           original_bytes=original_bytes)
                        st.warning(f"Conversion to PDF unavailable for {uf.name}. Will send original bytes as fallback.")
                    conv_list.append(cf)
                    added += 1
                except Exception as e:
                    log(f"Conversion error for {uf.name}: {e}", "warning")
            st.session_state.converted_files_pm = conv_list
            if added:
                st.success(f"Added {added} file(s).")

    st.subheader("📂 Files in queue")
    conv = st.session_state.get("converted_files_pm", [])
    if not conv:
        st.info("No files queued.")
    else:
        for idx, cf in enumerate(conv):
            cols = st.columns([4,1,1,1])
            with cols[0]:
                selkey = f"sel_file_{idx}"
                if selkey not in st.session_state:
                    st.session_state[selkey] = True
                st.checkbox(f"{cf.pdf_name} (orig: {cf.orig_name})", value=st.session_state[selkey], key=selkey)
                if st.button(f"Preview {idx}", key=f"preview_{idx}"):
                    blob = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
                    if blob:
                        b64 = base64.b64encode(blob).decode('utf-8')
                        ts = int(time.time()*1000)
                        js = f"""
                        <script>
                        (function(){{
                            const b64="{b64}";
                            const bytes=atob(b64);const arr=new Uint8Array(bytes.length);
                            for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                            const blob=new Blob([arr],{{type:'application/pdf'}});
                            const url=URL.createObjectURL(blob);
                            const w=window.open(url,'preview_{ts}','width=900,height=700');
                            if(!w)alert('Allow popups to preview.');
                        }})();
                        </script>
                        """
                        components.html(js, height=0)
                    else:
                        st.warning("No preview available.")
            with cols[1]:
                if st.button("Download", key=f"dl_{idx}"):
                    data = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
                    st.download_button("Download", data=data, file_name=cf.pdf_name, mime="application/pdf" if cf.pdf_bytes else "application/octet-stream", key=f"dlbtn_{idx}")
            with cols[2]:
                if st.button("Remove", key=f"rm_{idx}"):
                    st.session_state.converted_files_pm = [x for x in st.session_state.converted_files_pm if x.orig_name != cf.orig_name]
                    set_status(f"Removed {cf.orig_name}")
            with cols[3]:
                blob_for_count = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b'')
                pages = count_pdf_pages(blob_for_count)
                st.caption(f"{pages}p")

        selected_files = [cf for i,cf in enumerate(conv) if st.session_state.get(f"sel_file_{i}", True)]

        st.markdown("---")
        st.markdown("### 🖨️ Job Settings")
        col1, col2 = st.columns(2)
        with col1:
            copies = st.number_input("Copies per file", min_value=1, max_value=10, value=1, key="pm_job_copies")
        with col2:
            color_mode = st.selectbox("Color mode", options=["Auto", "Color", "Monochrome"], key="pm_job_colormode")

        if st.button("📤 Send Selected Files"):
            if not selected_files:
                st.error("No files selected.")
            else:
                threading.Thread(target=send_multiple_files, args=(selected_files, copies, color_mode), daemon=True).start()

    # status & payment
    if st.session_state.get("status"):
        st.info(f"📊 Status: {st.session_state['status']}")

    payinfo = st.session_state.get("payinfo")
    if payinfo and not st.session_state.get("process_complete"):
        st.markdown("---")
        st.markdown("## 💳 Payment")
        c1, c2 = st.columns(2)
        with c1:
            st.write(f"**File(s):** {payinfo.get('file_name', 'Multiple')}")
            st.write(f"**Amount:** ₹{payinfo.get('amount', 0)} {payinfo.get('currency', 'INR')}")
            st.write(f"**Pages:** {payinfo.get('pages', 'N/A')}")
        with c2:
            st.write(f"**Copies:** {payinfo.get('copies', 1)}")
            if payinfo.get("estimated"):
                st.warning("This is a local estimate while waiting for official payinfo from receiver.")
                if st.button("Use estimated payment now"):
                    pay_online()
            else:
                if st.button("Pay Online"):
                    pay_online()
                if st.button("Pay Offline"):
                    pay_offline()

    if st.session_state.get("process_complete"):
        st.success("🎉 Process Complete!")
        if st.button("Start New Transfer"):
            st.session_state.process_complete = False
            st.session_state.payinfo = None
            st.session_state.status = ""
            st.session_state.user_id = str(uuid.uuid4())[:8]
            set_status("Ready")

else:
    # Convert & Format page
    st.header("📄 Convert & Format")
    uploaded = st.file_uploader("Upload files to convert", accept_multiple_files=True,
                                type=['txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx','pdf'],
                                key="conv_upload")
    if uploaded:
        with st.spinner("Converting..."):
            converted = []
            for uf in uploaded:
                pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                if pdf_bytes:
                    converted.append({
                        "orig_name": uf.name,
                        "pdf_name": os.path.splitext(uf.name)[0] + ".pdf",
                        "pdf_bytes": pdf_bytes,
                        "pdf_base64": base64.b64encode(pdf_bytes).decode('utf-8')
                    })
                else:
                    st.error(f"Conversion failed: {uf.name}")
            if converted:
                st.session_state.converted_files_conv = converted
                st.success(f"Converted {len(converted)} files.")

    if st.session_state.get("converted_files_conv"):
        st.subheader("Converted Items")
        for i, it in enumerate(st.session_state.converted_files_conv):
            cols = st.columns([3,1,1])
            cols[0].write(f"**{it['pdf_name']}**")
            cols[0].caption(it['orig_name'])
            if cols[1].button("Preview", key=f"c_preview_{i}"):
                b64 = it['pdf_base64']; ts=int(time.time()*1000)
                js=f"""
                <script>
                (function(){{
                    const b64="{b64}";
                    const bytes=atob(b64);const arr=new Uint8Array(bytes.length);
                    for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                    const blob=new Blob([arr],{{type:'application/pdf'}});
                    const url=URL.createObjectURL(blob);
                    const w=window.open(url,'conv_preview_{ts}','width=900,height=700');
                    if(!w)alert('Allow popups to preview.');
                }})();
                </script>
                """
                components.html(js, height=0)
            if cols[2].button("Add to Print Queue", key=f"c_add_{i}"):
                cf = ConvertedFile(orig_name=it['orig_name'], pdf_name=it['pdf_name'], pdf_bytes=it['pdf_bytes'], settings=PrintSettings(), original_bytes=None)
                lst = st.session_state.get("converted_files_pm", [])
                lst.append(cf)
                st.session_state.converted_files_pm = lst
                st.success("Added to print queue.")

st.markdown("<div style='text-align:center;color:#666;padding-top:6px;'>Autoprint — Firestore chunked upload sender (docx fallback improved)</div>", unsafe_allow_html=True)
