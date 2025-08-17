# wo3_autoprint_firestore_sender.py — Full upgraded Streamlit sender (complete)
# Run: streamlit run wo3_autoprint_firestore_sender.py

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
    PdfReader = None
    PDF_READER_AVAILABLE = False

# Optional QR generation
try:
    import qrcode
    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False

# Optional auto-refresh helper (install via pip install streamlit-autorefresh)
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

# --------- Utilities ----------
def abspath(p: str) -> str:
    return os.path.abspath(p)

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
            log(f"Attempt {i+1}/{attempts} failed for {func.__name__}: {e}", "warning")
            logger.debug(traceback.format_exc())
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
    original_bytes: Optional[bytes] = None  # saved original upload bytes for fallback

# --------- FileConverter ----------
class FileConverter:
    SUPPORTED_TEXT_EXTENSIONS = {'.txt', '.md', '.rtf', '.html', '.htm'}
    SUPPORTED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp'}
    LIBREOFFICE_TIMEOUT = 60
    PANDOC_TIMEOUT = 50

    @classmethod
    def convert_text_to_pdf_bytes(cls, file_content: bytes, encoding='utf-8') -> Optional[bytes]:
        try:
            text = file_content.decode(encoding, errors='ignore')
            pdf = FPDF()
            pdf.add_page()
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.set_font("Helvetica", size=10)
            for line in text.splitlines():
                if len(line) > 200:
                    line = line[:197] + "..."
                pdf.cell(0, 5, txt=line, ln=1)
            return pdf.output(dest='S').encode('latin-1')
        except Exception as e:
            log(f"convert_text_to_pdf_bytes failed: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def convert_image_to_pdf_bytes(cls, file_content: bytes) -> Optional[bytes]:
        try:
            from io import BytesIO
            with Image.open(BytesIO(file_content)) as img:
                if img.size[0] > 2000 or img.size[1] > 2000:
                    img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                out = BytesIO()
                img.save(out, format='PDF', quality=85)
                return out.getvalue()
        except Exception as e:
            log(f"convert_image_to_pdf_bytes failed: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

    @classmethod
    def convert_docx_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        input_path = abspath(input_path)
        out_pdf = os.path.join(tempfile.gettempdir(), f"docx_out_{int(time.time()*1000)}.pdf")
        headless = system_is_headless()

        # Try docx2pdf if interactive environment and module available
        try:
            import docx2pdf
            DOCX2PDF_AVAILABLE = True
        except Exception:
            DOCX2PDF_AVAILABLE = False

        if not headless and DOCX2PDF_AVAILABLE:
            try:
                def _try_docx2pdf():
                    try:
                        docx2pdf.convert(input_path, os.path.dirname(out_pdf))
                    except TypeError:
                        docx2pdf.convert(input_path, out_pdf)
                    expected = os.path.join(os.path.dirname(out_pdf), os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                    if os.path.exists(expected) and expected != out_pdf:
                        os.replace(expected, out_pdf)
                    return os.path.exists(out_pdf)
                ok = retry_with_backoff(_try_docx2pdf, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception as e:
                log(f"docx2pdf failed: {e}", "warning")

        # Try win32com (Windows)
        try:
            import win32com.client as _win32com_client
            import pythoncom as _pywin_pythoncom
            WIN32COM_AVAILABLE = True
        except Exception:
            WIN32COM_AVAILABLE = False

        if platform.system() == "Windows" and WIN32COM_AVAILABLE:
            try:
                def _try_win():
                    try:
                        _pywin_pythoncom.CoInitialize()
                    except Exception:
                        pass
                    try:
                        word = _win32com_client.DispatchEx("Word.Application")
                        word.Visible = False
                        word.DisplayAlerts = 0
                        doc = word.Documents.Open(input_path, False, False, False)
                        wdFormatPDF = 17
                        doc.SaveAs(out_pdf, FileFormat=wdFormatPDF)
                        doc.Close(False)
                        try:
                            word.Quit()
                        except:
                            pass
                        return os.path.exists(out_pdf)
                    finally:
                        try:
                            _pywin_pythoncom.CoUninitialize()
                        except Exception:
                            pass
                ok = retry_with_backoff(_try_win, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception as e:
                log(f"win32com conversion failed: {e}", "warning")

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
                    expected = os.path.join(os.path.dirname(out_pdf), os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
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
            except Exception as e:
                log(f"LibreOffice conversion failed: {e}", "warning")

        # Try pandoc (last resort)
        try:
            import pypandoc
            PYPANDOC_AVAILABLE = True
        except Exception:
            PYPANDOC_AVAILABLE = False

        if PYPANDOC_AVAILABLE:
            try:
                def _try_pandoc():
                    pandoc_exec = find_executable(["pandoc"])
                    if not pandoc_exec:
                        raise FileNotFoundError("pandoc not found")
                    engine = None
                    if find_executable(["pdflatex"]):
                        engine = "pdflatex"
                    elif find_executable(["xelatex"]):
                        engine = "xelatex"
                    cmd = [pandoc_exec, input_path, "-o", out_pdf]
                    if engine:
                        cmd += [f"--pdf-engine={engine}"]
                    ok, out = run_subprocess(cmd, timeout=cls.PANDOC_TIMEOUT)
                    return ok and os.path.exists(out_pdf)
                ok = retry_with_backoff(_try_pandoc, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception as e:
                log(f"pandoc conversion failed: {e}", "warning")

        log("DOCX conversion failed (all backends)", "error")
        safe_remove(out_pdf)
        return None

    @classmethod
    def convert_pptx_to_pdf_bytes(cls, input_path: str) -> Optional[bytes]:
        input_path = abspath(input_path)
        out_pdf = os.path.join(tempfile.gettempdir(), f"pptx_out_{int(time.time()*1000)}.pdf")

        # Try Spire
        try:
            from spire.presentation import Presentation, FileFormat
            SPIRE_AVAILABLE = True
        except Exception:
            SPIRE_AVAILABLE = False

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
            except Exception as e:
                log(f"Spire.Presentation failed: {e}", "warning")

        # Try Windows COM
        try:
            import comtypes.client
            import pythoncom as _pythoncom
            COMTYPES_AVAILABLE = True
        except Exception:
            COMTYPES_AVAILABLE = False

        if platform.system() == "Windows" and COMTYPES_AVAILABLE:
            try:
                def _try_com():
                    try:
                        _pythoncom.CoInitialize()
                    except Exception:
                        pass
                    try:
                        ppt = comtypes.client.CreateObject("PowerPoint.Application")
                        ppt.Visible = 0
                        pres = ppt.Presentations.Open(input_path, 0, 0, 0)
                        pres.ExportAsFixedFormat(out_pdf, 2)
                        pres.Close()
                        ppt.Quit()
                        return os.path.exists(out_pdf)
                    finally:
                        try:
                            _pythoncom.CoUninitialize()
                        except Exception:
                            pass
                ok = retry_with_backoff(_try_com, attempts=2)
                if ok:
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception as e:
                log(f"PPTX COM failed: {e}", "warning")

        soffice = find_executable(["soffice", "libreoffice", "/usr/bin/libreoffice"])
        if soffice:
            try:
                def _try_libre():
                    cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(out_pdf), input_path]
                    ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                    expected = os.path.join(os.path.dirname(out_pdf), os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
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
            except Exception as e:
                log(f"LibreOffice PPTX failed: {e}", "warning")

        log("PPTX conversion failed (all backends)", "error")
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
                expected = os.path.join(os.path.dirname(out_pdf), os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
                if os.path.exists(expected):
                    if expected != out_pdf:
                        os.replace(expected, out_pdf)
                    with open(out_pdf, "rb") as f:
                        data = f.read()
                    safe_remove(out_pdf)
                    return data
            except Exception as e:
                log(f"LibreOffice generic failed: {e}", "warning")
        try:
            import pypandoc
            PYPANDOC_AVAILABLE = True
        except Exception:
            PYPANDOC_AVAILABLE = False

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
            except Exception as e:
                log(f"Pandoc generic failed: {e}", "warning")
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
                return cls.convert_image_to_pdf_bytes(content)
            if suffix in (".docx", ".pptx"):
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                    tf.write(content)
                    tf.flush()
                    tmpname = tf.name
                try:
                    if suffix == ".docx":
                        return cls.convert_docx_to_pdf_bytes(tmpname)
                    else:
                        return cls.convert_pptx_to_pdf_bytes(tmpname)
                finally:
                    safe_remove(tmpname)
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

# --------- Firestore sender utilities ----------
COLLECTION = st.secrets.get("collection_name", "files") if st.secrets else "files"
CHUNK_TEXT_SIZE = 900_000
MAX_BATCH_WRITE = 300

def init_db_from_secrets():
    sa_json = st.secrets.get("firebase_service_account") if st.secrets else None
    if sa_json:
        sa = json.loads(sa_json)
    else:
        fallback_path = st.secrets.get("service_account_file") if st.secrets else None
        if not fallback_path:
            raise RuntimeError("Provide firebase_service_account in Streamlit secrets or service_account_file path.")
        with open(fallback_path, "r", encoding="utf-8") as f:
            sa = json.load(f)
    if "private_key" in sa and isinstance(sa["private_key"], str):
        sa["private_key"] = sa["private_key"].replace("\\n", "\n")
    try:
        app = firebase_admin.get_app()
    except ValueError:
        cred = credentials.Certificate(sa)
        app = firebase_admin.initialize_app(cred)
    return firestore.client(app=app)

# initialize Firestore client
try:
    db = init_db_from_secrets()
    st.success("✅ Firebase initialized")
except Exception as e:
    st.error("❌ Firebase init failed: " + str(e))
    st.stop()

# helper crypto / encode
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def compress_and_encode_bytes(b: bytes) -> str:
    return base64.b64encode(zlib.compress(b)).decode("utf-8")

def chunk_text(text: str, size: int = CHUNK_TEXT_SIZE) -> List[str]:
    return [text[i:i+size] for i in range(0, len(text), size)]

# thread-safe queue for listener -> main UI
ACK_QUEUE = queue.Queue()

# upload a single file (chunks + per-file meta) — includes user_name/user_id in per-file manifest
def send_file_to_firestore(file_bytes: bytes, file_name: str, user_name: str = "", user_id: str = "") -> Tuple[str, int]:
    file_sha = sha256_bytes(file_bytes)
    full_b64 = compress_and_encode_bytes(file_bytes)
    chunks = chunk_text(full_b64, CHUNK_TEXT_SIZE)
    total_chunks = len(chunks)
    file_id = str(uuid.uuid4())

    batch = db.batch()
    written = 0
    for idx, piece in enumerate(chunks):
        doc_ref = db.collection(COLLECTION).document(f"{file_id}_{idx}")
        batch.set(doc_ref, {
            "file_name": file_name,
            "chunk_index": idx,
            "total_chunks": total_chunks,
            "data": piece
        })
        written += 1
        if written % MAX_BATCH_WRITE == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()

    # create per-file manifest (include uploader info)
    meta_ref = db.collection(COLLECTION).document(f"{file_id}_meta")
    meta_payload = {
        "file_id": file_id,
        "file_name": file_name,
        "total_chunks": total_chunks,
        "sha256": file_sha,
        "size_bytes": len(file_bytes),
        "uploaded_at": firestore.SERVER_TIMESTAMP,
        "user_name": user_name or "",
        "user_id": user_id or ""
    }
    meta_ref.set(meta_payload)
    return file_id, total_chunks

# create job manifest (multi-file) and write to Firestore — returns job_id and job_files
def send_job_to_firestore(files: List[Dict[str, Any]], user_name: str = "", user_id: str = "") -> Tuple[str, List[Dict[str, Any]]]:
    job_id = str(uuid.uuid4())
    job_files = []
    progress = st.progress(0)
    total_files = len(files)
    if total_files == 0:
        raise ValueError("No files to upload")

    for idx, f in enumerate(files):
        file_bytes = f.get("file_bytes") or b""
        file_name = f.get("file_name") or f"file_{int(time.time())}.pdf"
        pages = f.get("pages") or count_pdf_pages(file_bytes)
        settings = f.get("settings") or {}
        st.info(f"Uploading file {idx+1}/{total_files}: {file_name}")
        # pass user info to per-file meta so receiver can see uploader name
        fid, total_chunks = send_file_to_firestore(file_bytes, file_name, user_name=user_name, user_id=user_id)
        job_files.append({
            "file_id": fid,
            "file_name": file_name,
            "size_bytes": len(file_bytes),
            "pages": pages,
            "total_chunks": total_chunks,
            "settings": settings,
            "will_send_converted": True
        })
        progress.progress(int(((idx+1) / total_files) * 100))

    # job manifest
    job_meta = {
        "job_id": job_id,
        "file_count": len(job_files),
        "files": job_files,
        "user_name": user_name or "",
        "user_id": user_id or "",
        "timestamp": firestore.SERVER_TIMESTAMP,
        "transfer_mode": "file_share",
    }
    db.collection(COLLECTION).document(f"{job_id}_meta").set(job_meta)

    try:
        db.collection("health_check").document("last_job").set({"job_id": job_id, "ts": firestore.SERVER_TIMESTAMP})
    except Exception:
        pass
    progress.empty()
    return job_id, job_files

# attach listener on job manifest to receive payinfo and final ack updates
def attach_job_listener(job_id: str):
    doc_ref = db.collection(COLLECTION).document(f"{job_id}_meta")
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
            # if payinfo field present, push to ACK_QUEUE
            if "payinfo" in data:
                ACK_QUEUE.put(("payinfo", data.get("payinfo")))
                pi = data.get("payinfo") or {}
                if isinstance(pi, dict) and (pi.get("paid") or pi.get("status") in ("paid","completed","received")):
                    ACK_QUEUE.put(("payment", {"job_id": job_id, "payinfo": pi}))
            # if top-level flags/fields indicating payment present
            if data.get("payment_received") is True or data.get("payment_status") in ("paid","completed","received"):
                ACK_QUEUE.put(("payment", {"job_id": job_id, "payload": data}))
            if "final_acks" in data:
                for a in (data.get("final_acks") or []):
                    ACK_QUEUE.put(("ack", a))
            if "order_id" in data and "amount" in data:
                ACK_QUEUE.put(("payinfo", {
                    "order_id": data.get("order_id"),
                    "amount": data.get("amount"),
                    "currency": data.get("currency"),
                    "owner_upi": data.get("owner_upi"),
                    "file_name": data.get("file_name"),
                    "pages": data.get("pages"),
                    "copies": data.get("copies"),
                    "status": data.get("status", "queued")
                }))
            if "status" in data:
                ACK_QUEUE.put(("status", {"status": data.get("status"), "job_id": job_id}))
        except Exception:
            logger.debug("job listener exception:\n" + traceback.format_exc())

    listener = doc_ref.on_snapshot(callback)
    st.session_state["job_listener"] = listener
    set_status(f"Listening for job updates: {job_id}")

def detach_job_listener():
    listener = st.session_state.get("job_listener")
    if listener:
        try:
            listener.unsubscribe()
        except Exception:
            pass
    st.session_state["job_listener"] = None

# Attach listener to per-file manifest (so sender sees payinfo written by receiver into file meta)
def attach_file_listener(file_id: str):
    try:
        doc_ref = db.collection(COLLECTION).document(f"{file_id}_meta")
        def cb(doc_snapshot, changes, read_time):
            try:
                doc = None
                if isinstance(doc_snapshot, list) and len(doc_snapshot) > 0:
                    doc = doc_snapshot[0]
                else:
                    doc = doc_snapshot
                if doc is None or not doc.exists:
                    return
                data = doc.to_dict() or {}
                if "payinfo" in data:
                    ACK_QUEUE.put(("payinfo", data.get("payinfo")))
                    pi = data.get("payinfo") or {}
                    if isinstance(pi, dict) and (pi.get("paid") or pi.get("status") in ("paid","completed","received")):
                        ACK_QUEUE.put(("payment", {"file_id": file_id, "payinfo": pi}))
                if data.get("payment_received") is True or data.get("payment_status") in ("paid","completed","received"):
                    ACK_QUEUE.put(("payment", {"file_id": file_id, "payload": data}))
                if "order_id" in data and "amount" in data:
                    ACK_QUEUE.put(("payinfo", {
                        "order_id": data.get("order_id"),
                        "amount": data.get("amount"),
                        "currency": data.get("currency"),
                        "owner_upi": data.get("owner_upi"),
                        "file_name": data.get("file_name"),
                        "pages": data.get("pages"),
                        "copies": data.get("copies"),
                        "status": data.get("status", "queued")
                    }))
            except Exception:
                logger.debug("file listener exception:\n" + traceback.format_exc())

        listener = doc_ref.on_snapshot(cb)
        ss_key = "file_listeners"
        if ss_key not in st.session_state:
            st.session_state[ss_key] = {}
        st.session_state[ss_key][file_id] = listener
        set_status(f"Listening for file updates: {file_id}")
    except Exception:
        logger.debug("attach_file_listener failed:\n" + traceback.format_exc())

def detach_file_listeners():
    for d in list((st.session_state.get("file_listeners") or {}).items()):
        fid, listener = d
        try:
            listener.unsubscribe()
        except Exception:
            pass
    st.session_state["file_listeners"] = {}

# --------- Enhanced Payment Functions ----------
def validate_payment_info(payinfo: dict) -> bool:
    """Validate that payinfo contains required UPI ID and amount"""
    if not payinfo:
        return False
    
    upi_id = payinfo.get("owner_upi", "").strip()
    amount = payinfo.get("amount", 0)
    
    # Validate UPI ID format (basic check)
    if not upi_id or "@" not in upi_id:
        return False
    
    # Validate amount
    try:
        amount = float(amount)
        if amount <= 0:
            return False
    except (ValueError, TypeError):
        return False
    
    return True

def generate_upi_uri(upi_id: str, amount: float, note: str = None) -> str:
    """Generate UPI payment URI"""
    params = [
        f"pa={quote_plus(upi_id)}", 
        f"am={quote_plus(str(amount))}"
    ]
    if note:
        params.append(f"tn={quote_plus(note)}")
    return "upi://pay?" + "&".join(params)

def show_payment_ui(payinfo: dict):
    """Enhanced payment UI with proper validation and flow"""
    if not validate_payment_info(payinfo):
        st.error("❌ **Invalid payment information received**")
        st.write("Please contact support or try again.")
        if st.button("🔄 Cancel & Retry"):
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

    st.markdown("---")
    st.markdown("## 💳 **Payment Required**")
    
    # Payment details in a nice layout
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
    st.markdown("### 🎯 **Choose Payment Method**")
    
    # Payment method buttons
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 📱 **Pay Online (Recommended)**")
        st.write("• Instant payment via UPI apps")
        st.write("• Automatic confirmation")
        st.write("• QR code available")
        
        if st.button("💳 **Pay Online**", type="primary", use_container_width=True, key="enhanced_pay_online"):
            handle_online_payment(upi_id, amount, file_name)
    
    with col2:
        st.markdown("#### 💵 **Pay Offline**")
        st.write("• Pay directly to the shop owner")
        st.write("• Manual confirmation required")
        st.write("• Cash or other methods")
        
        if st.button("💵 **Pay Offline**", use_container_width=True, key="enhanced_pay_offline"):
            handle_offline_payment(amount, currency)

    # Cancel option
    st.markdown("---")
    col1, col2, col3 = st.columns([1,1,1])
    with col2:
        if st.button("❌ **Cancel Payment**", key="enhanced_cancel_payment"):
            cancel_payment()

def handle_online_payment(upi_id: str, amount: float, file_name: str):
    """Handle online UPI payment with enhanced UI"""
    # Generate UPI URI
    upi_uri = generate_upi_uri(upi_id, amount, note=f"Print: {file_name}")
    
    # Mark payment attempt in Firestore
    mark_payment_attempt("upi_intent")
    
    # Show payment interface
    st.markdown("### 📱 **Complete UPI Payment**")
    
    # Payment button with URI
    st.markdown(f"""
    <div style="text-align: center; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 10px; margin: 10px 0;">
        <h3 style="color: white; margin-bottom: 15px;">💳 Pay ₹{amount:.2f}</h3>
        <a href="{upi_uri}" target="_blank" style="display: inline-block; padding: 12px 30px; background: white; color: #667eea; text-decoration: none; border-radius: 25px; font-weight: bold; box-shadow: 0 4px 15px rgba(0,0,0,0.2);">
            🚀 Open Payment App
        </a>
    </div>
    """, unsafe_allow_html=True)
    
    # QR Code if available
    if QR_AVAILABLE:
        try:
            col1, col2, col3 = st.columns([1,2,1])
            with col2:
                qr = qrcode.QRCode(version=1, box_size=8, border=2)
                qr.add_data(upi_uri)
                qr.make(fit=True)
                
                img = qr.make_image(fill_color="black", back_color="white")
                st.image(img, caption="📱 Scan with any UPI app (GPay, PhonePe, Paytm, etc.)", use_container_width=False)
        except Exception as e:
            log(f"QR code generation failed: {e}", "warning")
    
    # Auto-open payment app
    try:
        webbrowser.open(upi_uri)
    except Exception:
        pass
    
    # Payment status
    st.info("🕐 **Payment in progress...**")
    st.markdown("""
    **📋 Instructions:**
    1. 📱 Click "Open Payment App" or scan the QR code
    2. 💳 Complete the payment in your UPI app
    3. ⏳ Wait for automatic confirmation (usually takes a few seconds)
    4. ✅ You'll see a success message once confirmed
    """)
    
    # Set waiting state
    st.session_state["waiting_for_payment"] = True
    st.session_state["process_complete"] = False

def handle_offline_payment(amount: float, currency: str):
    """Handle offline payment with enhanced confirmation"""
    # Mark offline payment in Firestore
    mark_payment_attempt("offline")
    mark_payment_completed("offline")
    
    # Show success message
    st.balloons()
    
    st.markdown(f"""
    <div style="text-align: center; padding: 30px; background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%); border-radius: 15px; color: white; margin: 20px 0;">
        <h2>🎉 Payment Confirmed!</h2>
        <h3>💵 Please pay ₹{amount:.2f} {currency} to the shop owner</h3>
        <p style="font-size: 18px; margin: 15px 0;">✅ Your print job is now queued for processing</p>
    </div>
    """, unsafe_allow_html=True)
    
    st.success("✅ **Thank you for using our service!**")
    st.markdown("""
    ### 📝 **Next Steps:**
    - 💰 Pay the shop owner directly
    - 🖨️ Your files will be printed shortly
    - 📞 Contact the shop if you have any questions
    """)
    
    # Complete the process
    complete_payment_process()

def mark_payment_attempt(method: str):
    """Mark payment attempt in Firestore for all current files"""
    job_file_ids = st.session_state.get("current_file_ids", [])
    user_id = st.session_state.get("user_id", "")
    
    for fid in job_file_ids:
        try:
            db.collection(COLLECTION).document(f"{fid}_meta").update({
                "payment_attempted_by": user_id,
                "payment_attempt_time": firestore.SERVER_TIMESTAMP,
                "payment_method": method
            })
        except Exception as e:
            log(f"Failed to mark payment attempt for {fid}: {e}", "warning")

def mark_payment_completed(method: str):
    """Mark payment as completed in Firestore"""
    job_file_ids = st.session_state.get("current_file_ids", [])
    user_id = st.session_state.get("user_id", "")
    
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
            log(f"Failed to mark payment completion for {fid}: {e}", "warning")

def complete_payment_process():
    """Complete the payment process and clean up"""
    set_status("Payment process completed successfully")
    
    # Update session state
    st.session_state["payinfo"] = None
    st.session_state["process_complete"] = True
    st.session_state["waiting_for_payment"] = False
    
    # Clean up listeners
    detach_file_listeners()
    detach_job_listener()
    
    # Clear job references
    st.session_state["current_job_id"] = None
    st.session_state["current_file_ids"] = []

def cancel_payment():
    """Cancel the payment process"""
    set_status("Payment cancelled by user")
    
    # Clean up listeners
    detach_job_listener()
    detach_file_listeners()
    
    # Reset session state
    st.session_state["payinfo"] = None
    st.session_state["current_job_id"] = None
    st.session_state["current_file_ids"] = []
    st.session_state["waiting_for_payment"] = False
    st.session_state["process_complete"] = False
    
    st.warning("Payment cancelled. You can start a new print job.")

# --------- Streamlit UI initialization keys ----------
if 'converted_files_pm' not in st.session_state:
    st.session_state.converted_files_pm = []
if 'converted_files_conv' not in st.session_state:
    st.session_state.converted_files_conv = []
if 'formatted_pdfs' not in st.session_state:
    st.session_state.formatted_pdfs = {}

if "payinfo" not in st.session_state:
    st.session_state["payinfo"] = None
if "status" not in st.session_state:
    st.session_state["status"] = ""
if "process_complete" not in st.session_state:
    st.session_state["process_complete"] = False
if "user_name" not in st.session_state:
    st.session_state["user_name"] = ""
if "user_id" not in st.session_state:
    st.session_state["user_id"] = str(uuid.uuid4())[:8]
if "print_ack" not in st.session_state:
    st.session_state["print_ack"] = None
if "job_listener" not in st.session_state:
    st.session_state["job_listener"] = None
if "current_job_id" not in st.session_state:
    st.session_state["current_job_id"] = None
if "current_file_ids" not in st.session_state:
    st.session_state["current_file_ids"] = []
if "waiting_for_payment" not in st.session_state:
    st.session_state["waiting_for_payment"] = False
if "file_listeners" not in st.session_state:
    st.session_state["file_listeners"] = {}

def set_status(s):
    st.session_state["status"] = f"{datetime.datetime.now().strftime('%H:%M:%S')} - {s}"

# Attach job listener
def start_job_listener(job_id: str):
    detach_job_listener()
    st.session_state["current_job_id"] = job_id
    attach_job_listener(job_id)
    set_status(f"Listening for job updates: {job_id}")

# Process ACK_QUEUE into session_state with enhanced handling
def process_ack_queue():
    changed = False
    try:
        while True:
            typ, payload = ACK_QUEUE.get_nowait()
            if typ == "payinfo":
                st.session_state["payinfo"] = payload
                set_status("Payment information received from printer")
                changed = True
            elif typ == "ack":
                st.session_state["print_ack"] = payload
                set_status(f"Print completed: {payload.get('status', 'unknown')}")
                changed = True
            elif typ == "status":
                job_id = payload.get('job_id', '')
                status = payload.get('status', '')
                st.session_state["status"] = f"{datetime.datetime.now().strftime('%H:%M:%S')} - Job {job_id[:8]}... status: {status}"
                changed = True
            elif typ == "payment":
                # Payment confirmed by receiver
                set_status("Payment confirmed by printer!")
                st.success("✅ **Payment confirmed - thank you!**")
                st.balloons()
                complete_payment_process()
                changed = True
    except queue.Empty:
        pass
    return changed

# send_multiple_files_firestore (unchanged)
def send_multiple_files_firestore(converted_files: List[ConvertedFile], copies: int, color_mode: str):
    if not converted_files:
        st.error("No files selected to send.")
        return

    set_status("Preparing upload to Firestore...")
    files_payload = []
    total_bytes = 0
    for cf in converted_files:
        blob = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
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

    try:
        job_id, job_files = send_job_to_firestore(files_payload, user_name=st.session_state.get("user_name", ""), user_id=st.session_state.get("user_id", ""))
        set_status(f"Job uploaded successfully: {job_id}")
        st.success(f"✅ **Print job created:** `{job_id}`")
        
        # Record job and file ids in session so payment actions can reference them
        st.session_state["current_job_id"] = job_id
        st.session_state["current_file_ids"] = [f["file_id"] for f in job_files]
        
        # Attach listeners to job and file metadata
        start_job_listener(job_id)
        for f in job_files:
            try:
                attach_file_listener(f["file_id"])
            except Exception:
                logger.debug("attach_file_listener error:\n" + traceback.format_exc())
        
        # Reset payment-related state
        st.session_state["payinfo"] = None
        st.session_state["print_ack"] = None
        st.session_state["process_complete"] = False
        st.session_state["waiting_for_payment"] = False
        
    except Exception as e:
        st.error(f"❌ Upload failed: {e}")
        set_status("Upload failed")
        logger.debug(traceback.format_exc())
        return

# ---------------- Enhanced Streamlit UI ----------------
st.set_page_config(
    page_title="Autoprint (Firestore Sender)", 
    layout="wide", 
    page_icon="🖨️", 
    initial_sidebar_state="expanded"
)

st.markdown(
    """
    <style>
      .appview-container .main .block-container {padding-top: 10px; padding-bottom:10px;}
      .stButton>button {padding:8px 12px; font-weight: 500;}
      .stDownloadButton>button {padding:8px 12px;}
      .stProgress {height:16px;}
      
      /* Enhanced payment UI styles */
      .payment-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 20px;
        border-radius: 15px;
        color: white;
        text-align: center;
        margin: 15px 0;
      }
      
      .success-card {
        background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
        padding: 20px;
        border-radius: 15px;
        color: white;
        text-align: center;
        margin: 15px 0;
      }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown("<h1 style='text-align:center;margin:6px 0 8px 0;'>🖨️ Autoprint Service</h1>", unsafe_allow_html=True)
st.markdown("<p style='text-align:center;color:#666;'>Professional Document Printing with UPI Payment Integration</p>", unsafe_allow_html=True)

# Render Print Manager page with enhanced UI
def render_print_manager_page():
    # Auto-refresh functionality
    if AUTORELOAD_AVAILABLE:
        st_autorefresh(interval=2000, key="auto_refresh_sender", limit=1000)
    else:
        # Fallback refresh for active jobs
        if st.session_state.get("current_job_id") or st.session_state.get("waiting_for_payment"):
            js = """
            <script>
              function reloadIfVisible() {
                if (document.visibilityState === 'visible') {
                  window.location.reload();
                }
              }
              setInterval(reloadIfVisible, 3000);
            </script>
            """
            components.html(js, height=0)

    # Process real-time updates
    process_ack_queue()

    st.header("📄 Print Service Dashboard")
    
    # Show current status if active
    if st.session_state.get("status"):
        if st.session_state.get("waiting_for_payment"):
            st.info(f"🕐 **Status:** {st.session_state['status']} - Waiting for payment confirmation...")
        else:
            st.info(f"📊 **Status:** {st.session_state['status']}")

    # User information section
    st.markdown("### 👤 User Information")
    user_name = st.text_input(
        "Your name (optional)", 
        value=st.session_state.get("user_name", ""), 
        placeholder="Enter your name for print identification", 
        help="This helps identify your print job at the printer"
    )
    if user_name != st.session_state.get("user_name", ""):
        st.session_state["user_name"] = user_name
    
    st.caption(f"🆔 **Your ID:** `{st.session_state['user_id']}` (auto-generated)")

    # File upload section
    st.markdown("### 📁 Upload Files")
    uploaded = st.file_uploader(
        "Select files to print (supports PDF, Word, PowerPoint, images, text files)", 
        accept_multiple_files=True, 
        type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'], 
        key="pm_multi_upload"
    )
    
    if uploaded:
        with st.spinner("🔄 Converting files to PDF..."):
            conv_list = st.session_state.get("converted_files_pm", [])
            new_files = 0
            
            for uf in uploaded:
                if any(x.orig_name == uf.name for x in conv_list):
                    continue
                try:
                    original_bytes = uf.getvalue()
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                    if pdf_bytes:
                        cf = ConvertedFile(
                            orig_name=uf.name, 
                            pdf_name=os.path.splitext(uf.name)[0] + ".pdf", 
                            pdf_bytes=pdf_bytes, 
                            settings=PrintSettings(), 
                            original_bytes=original_bytes
                        )
                    else:
                        cf = ConvertedFile(
                            orig_name=uf.name, 
                            pdf_name=uf.name, 
                            pdf_bytes=b"", 
                            settings=PrintSettings(), 
                            original_bytes=original_bytes
                        )
                    conv_list.append(cf)
                    new_files += 1
                except Exception as e:
                    log(f"Conversion failed for {uf.name}: {e}", "warning")
                    st.error(f"⚠️ Failed to convert {uf.name}")
            
            if new_files > 0:
                st.session_state.converted_files_pm = conv_list
                st.success(f"✅ Added {new_files} file(s) to print queue")

    # File queue display
    st.markdown("### 📋 Print Queue")
    conv = st.session_state.get("converted_files_pm", [])
    
    if not conv:
        st.info("📝 No files in queue. Upload files above to get started.")
    else:
        # File list with enhanced UI
        for idx, cf in enumerate(conv):
            with st.expander(f"📄 {cf.pdf_name} (original: {cf.orig_name})", expanded=False):
                cols = st.columns([3,1,1,1,1])
                
                with cols[0]:
                    checked_key = f"sel_file_{idx}"
                    if checked_key not in st.session_state:
                        st.session_state[checked_key] = True
                    st.checkbox("Include in print job", value=st.session_state[checked_key], key=checked_key)
                
                with cols[1]:
                    if st.button("👁️ Preview", key=f"preview_pm_{idx}"):
                        if cf.pdf_bytes:
                            b64 = base64.b64encode(cf.pdf_bytes).decode('utf-8')
                            ts = int(time.time()*1000)
                            js = f"""
                            <script>
                            (function(){{
                                const b64="{b64}";
                                const bytes=atob(b64);const arr=new Uint8Array(bytes.length);
                                for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                                const blob=new Blob([arr],{{type:'application/pdf'}});
                                const url=URL.createObjectURL(blob);
                                const w=window.open(url,'preview_{ts}','width=900,height=700,scrollbars=yes,resizable=yes');
                                if(!w)alert('Please allow popups to preview files.');
                            }})();
                            </script>
                            """
                            components.html(js, height=0)
                        else:
                            st.warning("⚠️ PDF preview not available")
                
                with cols[2]:
                    if cf.pdf_bytes:
                        st.download_button("📥 PDF", data=cf.pdf_bytes, file_name=cf.pdf_name, mime="application/pdf", key=f"dl_{idx}")
                    else:
                        st.download_button("📥 Original", data=cf.original_bytes or b"", file_name=cf.orig_name, key=f"dl_orig_{idx}")
                
                with cols[3]:
                    if st.button("🗑️ Remove", key=f"rm_{idx}"):
                        st.session_state.converted_files_pm = [x for x in conv if x.orig_name != cf.orig_name]
                        st.rerun()
                
                with cols[4]:
                    blob_for_count = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b'')
                    pages = count_pdf_pages(blob_for_count)
                    st.metric("Pages", pages)

        # Print settings and submission
        selected_files = [cf for idx,cf in enumerate(conv) if st.session_state.get(f"sel_file_{idx}", True)]
        
        if selected_files:
            st.markdown("---")
            st.markdown("### ⚙️ Print Settings")
            
            col1, col2 = st.columns(2)
            with col1:
                copies = st.number_input("📑 Copies per file", min_value=1, max_value=10, value=1, key="pm_job_copies")
            with col2:
                color_mode = st.selectbox("🎨 Print Mode", options=["Auto", "Color", "Monochrome"], key="pm_job_colormode")
            
            # Calculate totals
            total_pages = sum(count_pdf_pages(cf.pdf_bytes or cf.original_bytes or b'') for cf in selected_files)
            total_copies = total_pages * copies
            
            st.info(f"📊 **Summary:** {len(selected_files)} files, {total_pages} pages, {total_copies} total pages to print")
            
            # Submit button
            if st.button("🖨️ **Send Print Job**", type="primary", use_container_width=True, key="pm_send_multi"):
                send_multiple_files_firestore(selected_files, copies, color_mode)

    # Process any new updates
    process_ack_queue()

    # Print acknowledgment display
    if st.session_state.get("print_ack"):
        ack = st.session_state["print_ack"]
        status = ack.get('status', 'unknown')
        note = ack.get('note', '')
        
        if status.lower() in ['completed', 'success', 'printed']:
            st.success(f"🎉 **Print completed successfully!** {note}")
        elif status.lower() in ['error', 'failed']:
            st.error(f"❌ **Print failed:** {note}")
        else:
            st.info(f"📄 **Print status:** {status} - {note}")

    # Enhanced Payment UI - main feature
    payinfo = st.session_state.get("payinfo")
    if payinfo and not st.session_state.get("process_complete"):
        show_payment_ui(payinfo)

    # Process completion
    if st.session_state.get("process_complete"):
        st.markdown("---")
        st.markdown(
            """
            <div style="text-align: center; padding: 30px; background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%); border-radius: 15px; color: white; margin: 20px 0;">
                <h2>🎉 Process Complete!</h2>
                <p style="font-size: 18px;">Thank you for using our print service</p>
            </div>
            """, 
            unsafe_allow_html=True
        )
        
        if st.button("🔄 **Start New Print Job**", type="primary", use_container_width=True):
            # Reset all session state for new job
            st.session_state["process_complete"] = False
            st.session_state["payinfo"] = None
            st.session_state["status"] = ""
            st.session_state["print_ack"] = None
            st.session_state["current_job_id"] = None
            st.session_state["current_file_ids"] = []
            st.session_state["user_id"] = str(uuid.uuid4())[:8]
            st.session_state["waiting_for_payment"] = False
            st.session_state["converted_files_pm"] = []  # Clear file queue
            set_status("Ready for new print job")
            st.rerun()

# Convert & Format page (enhanced)
def render_convert_page():
    st.header("🔄 Convert & Format Documents")
    st.write("Batch convert documents to PDF format for printing. Converted files can be previewed and formatted before printing.")

    # File upload for conversion
    uploaded = st.file_uploader(
        "📁 Upload files to convert to PDF", 
        accept_multiple_files=True,
        type=['txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx','pdf'],
        key="conv_upload",
        help="Supported formats: Text files, Images, Word documents, PowerPoint presentations, and existing PDFs"
    )
    
    if uploaded:
        with st.spinner("🔄 Converting files..."):
            converted = []
            failed = []
            
            for uf in uploaded:
                try:
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
                    else:
                        failed.append(uf.name)
                except Exception as e:
                    failed.append(uf.name)
                    log(f"Conversion failed for {uf.name}: {e}", "error")
            
            if converted:
                st.session_state.converted_files_conv = converted
                st.success(f"✅ Successfully converted {len(converted)} files")
            
            if failed:
                st.error(f"❌ Failed to convert {len(failed)} files: {', '.join(failed)}")

    # Display converted files
    if st.session_state.get("converted_files_conv"):
        st.markdown("### 📄 Converted Files")
        
        # Summary stats
        total_files = len(st.session_state.converted_files_conv)
        total_pages = sum(item.get("pages", 1) for item in st.session_state.converted_files_conv)
        total_size = sum(item.get("size_mb", 0) for item in st.session_state.converted_files_conv)
        
        col1, col2, col3 = st.columns(3)
        col1.metric("📁 Files", total_files)
        col2.metric("📑 Total Pages", total_pages)
        col3.metric("💾 Total Size", f"{total_size:.1f} MB")
        
        st.markdown("---")
        
        # File list with actions
        for i, item in enumerate(st.session_state.converted_files_conv):
            with st.container():
                col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
                
                with col1:
                    st.write(f"**📄 {item['pdf_name']}**")
                    st.caption(f"Original: {item['orig_name']} | Pages: {item.get('pages', 1)} | Size: {item.get('size_mb', 0):.1f} MB")
                
                with col2:
                    if st.button("👁️ Preview", key=f"c_preview_{i}"):
                        b64 = item['pdf_base64']
                        ts = int(time.time()*1000)
                        js = f"""
                        <script>
                        (function(){{
                            const b64="{b64}";
                            const bytes=atob(b64);
                            const arr=new Uint8Array(bytes.length);
                            for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                            const blob=new Blob([arr],{{type:'application/pdf'}});
                            const url=URL.createObjectURL(blob);
                            const w=window.open(url,'conv_preview_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes,toolbar=yes');
                            if(!w)alert('Please allow popups to preview files.');
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
                            const pop = window.open(url,'conv_print_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes,toolbar=yes');
                            if(pop){{ 
                                setTimeout(()=>{{ 
                                    try{{ 
                                        pop.print(); 
                                    }}catch(e){{
                                        console.log('Print dialog may have been blocked');
                                    }} 
                                }}, 1500); 
                            }} else {{ 
                                alert('Please allow popups for direct printing.'); 
                            }}
                          }} catch(e){{ 
                            alert('Error opening print dialog: ' + e.message); 
                          }}
                        }})();
                        </script>
                        """
                        components.html(js, height=0)
                
                with col5:
                    if st.button("🗑️ Remove", key=f"c_remove_{i}"):
                        st.session_state.converted_files_conv.pop(i)
                        st.rerun()
                
                st.markdown("---")
        
        # Bulk actions
        if st.session_state.converted_files_conv:
            st.markdown("### 🔧 Bulk Actions")
            col1, col2, col3 = st.columns(3)
            
            with col1:
                if st.button("📥 **Download All as ZIP**", use_container_width=True):
                    # This would require implementing ZIP creation
                    st.info("💡 ZIP download feature - coming soon!")
            
            with col2:
                if st.button("➕ **Add All to Print Queue**", use_container_width=True):
                    # Add all converted files to print manager queue
                    pm_files = st.session_state.get("converted_files_pm", [])
                    added = 0
                    
                    for item in st.session_state.converted_files_conv:
                        # Check if file already exists in PM queue
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
                        st.info("ℹ️ All files are already in the print queue")
            
            with col3:
                if st.button("🗑️ **Clear All**", use_container_width=True):
                    st.session_state.converted_files_conv = []
                    st.success("🗑️ Cleared all converted files")
                    st.rerun()

# Main application
def main():
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
        st.markdown("## ℹ️ About")
        st.markdown("""
        **Autoprint Service** provides:
        - 📄 Document conversion to PDF
        - 🖨️ Professional printing services  
        - 💳 Secure UPI payment integration
        - 📱 Real-time status updates
        - 🔄 Multi-format file support
        """)
        
        # Show current session info
        if st.session_state.get("user_name"):
            st.markdown(f"**👤 User:** {st.session_state['user_name']}")
        st.markdown(f"**🆔 Session:** `{st.session_state.get('user_id', 'unknown')}`")
        
        # Show current job status
        if st.session_state.get("current_job_id"):
            st.markdown(f"**📋 Active Job:** `{st.session_state['current_job_id'][:8]}...`")
        
        # Emergency reset
        st.markdown("---")
        if st.button("🔄 **Reset Session**", help="Clear all data and start fresh"):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

    # Main content area
    if "Print Manager" in page:
        render_print_manager_page()
    else:
        render_convert_page()

    # Footer
    st.markdown("---")
    col1, col2, col3 = st.columns([1,2,1])
    with col2:
        st.markdown(
            "<div style='text-align:center;color:#666;padding:10px;'>🖨️ <b>Autoprint Service</b> - Professional Document Printing with UPI Integration</div>", 
            unsafe_allow_html=True
        )

if __name__ == "__main__":
    main()
