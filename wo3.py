# wo3_autoprint_fixed_pages.py — Autoprint — Page-count fix included
# Run: streamlit run wo3_autoprint_fixed_pages.py

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
import socket
import datetime
import uuid
import webbrowser
import threading
import io                                  # <-- added for BytesIO

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
    from PIL import Image as PILImage  # avoid name clash
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

try:
    import comtypes.client
    import pythoncom as _pythoncom
    COMTYPES_AVAILABLE = True
except Exception:
    COMTYPES_AVAILABLE = False

try:
    import win32com.client as _win32com_client
    import pythoncom as _pywin_pythoncom
    WIN32COM_AVAILABLE = True
except Exception:
    WIN32COM_AVAILABLE = False

try:
    from spire.presentation import Presentation, FileFormat
    SPIRE_AVAILABLE = True
except Exception:
    SPIRE_AVAILABLE = False

# --------- Logging (file only) ----------
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

# --------- FileConverter (unchanged) ----------
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

# --------- Print job helper ----------
class PrintJobManager:
    PRINT_PRESETS = {
        "Standard": PrintSettings(),
        "Draft": PrintSettings(copies=1, color_mode="Black & White", quality="Draft", collate=False),
        "High Quality Duplex": PrintSettings(duplex="Double-sided", quality="High")
    }

    @staticmethod
    def create_print_job(job_name: str, files: List[ConvertedFile]) -> Dict[str, Any]:
        return {
            "job_name": job_name,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_files": len(files),
            "total_size_bytes": sum(len(f.pdf_bytes) for f in files),
            "files": [
                {
                    "pdf_name": file.pdf_name,
                    "orig_name": file.orig_name,
                    "size_bytes": len(file.pdf_bytes),
                    "settings": file.settings.__dict__,
                    "pdf_base64": base64.b64encode(file.pdf_bytes).decode('utf-8')
                }
                for file in files
            ]
        }

# --------- Page counting helper (NEW) ----------
def count_pdf_pages(blob: Optional[bytes]) -> int:
    """
    Return number of pages for a PDF given as bytes.
    If parsing fails or PyPDF2 not available -> return 1 as safe fallback.
    """
    if not blob:
        return 1
    if not PDF_READER_AVAILABLE:
        return 1
    try:
        stream = io.BytesIO(blob)
        reader = PdfReader(stream)
        # PdfReader.pages is a sequence
        return len(reader.pages)
    except Exception:
        # log minimal debug info, but return fallback 1
        logger.debug("count_pdf_pages failed:\n" + traceback.format_exc())
        return 1

# --------- Streamlit layout & styles ----------
st.set_page_config(page_title="Autoprint", layout="wide", page_icon="🖨️", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
      .appview-container .main .block-container {padding-top: 10px; padding-bottom:10px;}
      .stButton>button {padding:6px 10px;}
      .stDownloadButton>button {padding:6px 10px;}
      .stProgress {height:14px;}
    </style>
    """,
    unsafe_allow_html=True
)

# Top title + tip (unchanged)
st.markdown("<h1 style='text-align:center;margin:6px 0 8px 0;'>Autoprint</h1>", unsafe_allow_html=True)

st.markdown(
    """
    <div style="display:flex;align-items:center;gap:10px;padding:8px;border-radius:6px;background:#f1f7ff;">
      <strong style="font-size:13px;">Tip:</strong>
      <span style="font-size:13px;">
        To edit the file into the format you want to print, go to the <strong>Convert & Format</strong> page. 
        Convert and upload the saved format here to print.
      </span>
    </div>
    """,
    unsafe_allow_html=True
)

# Initialize session state containers
if 'converted_files_pm' not in st.session_state:
    st.session_state.converted_files_pm = []
if 'converted_files_conv' not in st.session_state:
    st.session_state.converted_files_conv = []
if 'formatted_pdfs' not in st.session_state:
    st.session_state.formatted_pdfs = {}

# Sidebar
with st.sidebar:
    st.title("Autoprint (founder: KOTA ANUJ KUMAR)")
    st.markdown("**Founder:** Kota Anuj kumar")
    page = st.radio("Page", ["Print Manager", "Convert & Format"], index=0)
    st.markdown("---")
    st.caption("Print Manager is the default. Use Convert & Format to batch-convert files.")
    with st.expander("Environment (compact)", expanded=False):
        st.write(f"Platform: {platform.system()}")
        st.write("docx2pdf:", bool(DOCX2PDF_AVAILABLE))
        st.write("win32com:", bool(WIN32COM_AVAILABLE))
        st.write("LibreOffice on PATH:", bool(find_executable(["soffice", "libreoffice"])) )
        st.write("Log file:", LOGFILE)
        if st.button("Show log tail", key="show_log"):
            try:
                with open(LOGFILE, "r", encoding="utf-8") as lf:
                    st.code(lf.read()[-4000:])
            except Exception as e:
                st.error(f"Could not read log file: {e}")

# --------- Reworked Print Manager (multi-file + raw_stream) ----------
HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 9999
TIMEOUT_SECONDS = 20

# Session keys for print manager
if "sock" not in st.session_state:
    st.session_state["sock"] = None
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

def set_status(s):
    st.session_state["status"] = f"{datetime.datetime.now().strftime('%H:%M:%S')} - {s}"

def close_sock():
    sock = st.session_state.get("sock")
    if sock:
        try:
            sock.close()
        except Exception:
            pass
    st.session_state["sock"] = None
    set_status("Connection closed")

def generate_upi_uri(upi_id, amount, note=None):
    params = [f"pa={upi_id}", f"am={amount}"]
    if note:
        params.append(f"tn={note}")
    return "upi://pay?" + "&".join(params)

# background listener
def _listen_for_final_ack(sock, order_id, timeout=60):
    try:
        sock.settimeout(1.0)
        buf = bytearray()
        start = time.time()
        while time.time() - start < timeout:
            try:
                b = sock.recv(1)
                if not b:
                    break
                buf.extend(b)
                if b == b"\n":
                    try:
                        msg = json.loads(buf.decode("utf-8", errors="ignore").strip())
                    except Exception:
                        buf = bytearray()
                        continue
                    if isinstance(msg, dict) and msg.get("order_id") == order_id:
                        st.session_state["print_ack"] = msg
                        set_status(f"Final ack received: {msg.get('status')}")
                        return
                    buf = bytearray()
            except socket.timeout:
                pass
        set_status("No final ack received within timeout.")
    except Exception as e:
        set_status(f"Error listening for final ack: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
        st.session_state["sock"] = None

def send_multiple_files(converted_files: List[ConvertedFile], copies: int, color_mode: str):
    if not converted_files:
        st.error("No files selected to send.")
        return

    set_status("Connecting to print server...")
    try:
        sock = socket.create_connection((HOST_DEFAULT, PORT_DEFAULT), timeout=15)
        sock.settimeout(None)
        st.session_state["sock"] = sock

        job_id = str(uuid.uuid4())[:12]
        files_meta = []
        total_bytes = 0
        for cf in converted_files:
            blob = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
            size = len(blob)
            # USE helper to count pages correctly
            pages = count_pdf_pages(blob)
            file_id = str(uuid.uuid4())[:8]
            files_meta.append({
                "file_id": file_id,
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
                "will_send_converted": bool(cf.pdf_bytes)
            })
            total_bytes += size

        metadata = {
            "job_id": job_id,
            "file_count": len(files_meta),
            "total_size_bytes": total_bytes,
            "files": files_meta,
            "user_name": st.session_state.get("user_name") or "",
            "user_id": st.session_state.get("user_id"),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "transfer_mode": "raw_stream"
        }

        json_line = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False) + "\n"
        sock.sendall(json_line.encode("utf-8"))
        set_status("Metadata sent. Streaming files...")

        for cf_meta in files_meta:
            matching = next((c for c in converted_files if (c.pdf_name == cf_meta["filename"] and c.orig_name == cf_meta["orig_filename"])), None)
            if not matching:
                continue
            blob = matching.pdf_bytes if matching.pdf_bytes else (matching.original_bytes or b"")
            if blob:
                sock.sendall(blob)

        try:
            sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass

        set_status(f"Job '{job_id}' uploaded successfully ({total_bytes} bytes). Waiting for payment info...")

        sock.settimeout(TIMEOUT_SECONDS)
        recv_buf = bytearray()
        try:
            while True:
                b = sock.recv(1)
                if not b:
                    break
                recv_buf.extend(b)
                if b == b"\n" or len(recv_buf) > 200*1024:
                    break
        except socket.timeout:
            st.error("Timeout waiting for payment information")
            set_status("Payment info timeout")
            close_sock()
            return
        except Exception as e:
            st.error(f"Error receiving payment info: {e}")
            close_sock()
            return

        if not recv_buf:
            st.warning("No payment information received from server")
            set_status("No payment info received")
            close_sock()
            return

        try:
            payinfo = json.loads(recv_buf.decode("utf-8", errors="ignore").strip())
            st.session_state["payinfo"] = payinfo
            set_status("Payment information received")
            order_id = payinfo.get("order_id")
            if order_id:
                listener_thread = threading.Thread(target=_listen_for_final_ack, args=(sock, order_id, 120), daemon=True)
                listener_thread.start()
            else:
                close_sock()
            return
        except Exception as e:
            st.error(f"Invalid payment information received: {e}")
            set_status("Invalid payment info")
            close_sock()
            return

    except Exception as e:
        st.error(f"Upload failed: {e}")
        set_status("Upload failed")
        close_sock()

def pay_offline():
    payinfo = st.session_state.get("payinfo", {})
    amount = payinfo.get("amount", 0)
    currency = payinfo.get("currency", "INR")
    close_sock()
    st.success(f"💵 **Please pay ₹{amount} {currency} offline**")
    st.success("✅ **Thank you for using our service!**")
    st.balloons()
    st.session_state["payinfo"] = None
    st.session_state["process_complete"] = True

def pay_online():
    payinfo = st.session_state.get("payinfo", {})
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
    st.info("📱 **You will be redirected to your payment app. Complete the payment there.**")
    st.success("✅ **Thank you for using our service!**")
    st.balloons()
    close_sock()
    st.session_state["payinfo"] = None
    st.session_state["process_complete"] = True

def cancel_payment():
    close_sock()
    st.session_state["payinfo"] = None
    set_status("Cancelled by user")

# Render Print Manager page
def render_print_manager_page():
    st.header("📄 File Transfer & Print Service (Multi-file)")
    st.write("Upload files (multiple). Each is converted to PDF (best-effort) and stored. Select which files to send together as one job.")

    # user info
    st.markdown("### 👤 User Information")
    user_name = st.text_input("Your name (optional)", value=st.session_state.get("user_name", ""), placeholder="Enter your name for print identification", help="This helps identify your print job at the printer")
    if user_name != st.session_state.get("user_name", ""):
        st.session_state["user_name"] = user_name
    st.caption(f"Your ID: **{st.session_state['user_id']}** (auto-generated)")

    # multi-upload
    uploaded = st.file_uploader("📁 Upload files to add to queue (multiple)", accept_multiple_files=True, type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'], key="pm_multi_upload")
    if uploaded:
        with st.spinner("Converting and storing..."):
            conv_list = st.session_state.get("converted_files_pm", [])
            for uf in uploaded:
                if any(x.orig_name == uf.name for x in conv_list):
                    continue
                try:
                    original_bytes = uf.getvalue()
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                    if pdf_bytes:
                        cf = ConvertedFile(orig_name=uf.name, pdf_name=os.path.splitext(uf.name)[0] + ".pdf", pdf_bytes=pdf_bytes, settings=PrintSettings(), original_bytes=original_bytes)
                    else:
                        cf = ConvertedFile(orig_name=uf.name, pdf_name=uf.name, pdf_bytes=b"", settings=PrintSettings(), original_bytes=original_bytes)
                    conv_list.append(cf)
                except Exception as e:
                    log(f"Conversion on upload failed for {uf.name}: {e}", "warning")
            st.session_state.converted_files_pm = conv_list
            st.success(f"Added {len(uploaded)} file(s). Conversion attempted where possible.")

    # queue
    st.subheader("📂 Files in queue")
    conv = st.session_state.get("converted_files_pm", [])
    if not conv:
        st.info("No files in queue. Upload above.")
    else:
        for idx, cf in enumerate(conv):
            cols = st.columns([4,1,1,1])
            with cols[0]:
                checked_key = f"sel_file_{idx}"
                if checked_key not in st.session_state:
                    st.session_state[checked_key] = True
                st.checkbox(f"{cf.pdf_name} (orig: {cf.orig_name})", value=st.session_state[checked_key], key=checked_key)
                if st.button(f"Preview {idx}", key=f"preview_pm_{idx}"):
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
                            const w=window.open(url,'pm_preview_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes,toolbar=yes');
                            if(!w)alert('Allow popups to preview.');
                        }})();
                        </script>
                        """
                        components.html(js, height=0)
                    else:
                        st.warning("No converted PDF available for preview; original bytes will be sent instead.")
            with cols[1]:
                if st.button("Download", key=f"dl_pm_{idx}"):
                    if cf.pdf_bytes:
                        st.download_button("Download PDF", data=cf.pdf_bytes, file_name=cf.pdf_name, mime="application/pdf", key=f"dlpdf_{idx}")
                    else:
                        st.download_button("Download original", data=cf.original_bytes or b"", file_name=cf.orig_name, mime="application/octet-stream", key=f"dlorig_{idx}")
            with cols[2]:
                if st.button("Remove", key=f"rm_pm_{idx}"):
                    new_list = [x for x in st.session_state.converted_files_pm if x.orig_name != cf.orig_name]
                    st.session_state.converted_files_pm = new_list
                    set_status(f"Removed {cf.orig_name} from queue")
            with cols[3]:
                # use helper to count pages for the display as well
                blob_for_count = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b'')
                pages = count_pdf_pages(blob_for_count)
                st.caption(f"{pages}p")

        # gather selected
        selected_files = [cf for idx,cf in enumerate(conv) if st.session_state.get(f"sel_file_{idx}", True)]

        st.markdown("---")
        st.markdown("### 🖨️ Job-level Print Settings")
        col1, col2 = st.columns(2)
        with col1:
            copies = st.number_input("Number of copies (per file)", min_value=1, max_value=10, value=1, key="pm_job_copies")
        with col2:
            color_mode = st.selectbox("Print mode", options=["Auto", "Color", "Monochrome"], key="pm_job_colormode")

        if st.button("📤 **Send Selected Files**", type="primary", use_container_width=True, key="pm_send_multi"):
            if not selected_files:
                st.error("No files selected.")
            else:
                send_multiple_files(selected_files, copies, color_mode)

    # status & payment
    if st.session_state.get("status"):
        st.info(f"📊 **Status:** {st.session_state['status']}")

    if st.session_state.get("print_ack"):
        ack = st.session_state["print_ack"]
        st.success(f"🖨️ Print result: {ack.get('status')} — {ack.get('note','')}")

    payinfo = st.session_state.get("payinfo")
    if payinfo and not st.session_state.get("process_complete"):
        st.markdown("---")
        st.markdown("## 💳 **Payment Required**")
        col1, col2 = st.columns(2)
        with col1:
            st.write(f"**📄 File(s):** {payinfo.get('file_name', payinfo.get('filename', 'Multiple'))}")
            st.write(f"**💰 Amount:** ₹{payinfo.get('amount', 0)} {payinfo.get('currency', 'INR')}")
        with col2:
            st.write(f"**📑 Pages:** {payinfo.get('pages', 'N/A')}")
            st.write(f"**📇 Copies:** {payinfo.get('copies', 1)}")
        st.markdown("### Choose Payment Method:")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("💳 **Pay Online**", type="primary", use_container_width=True, key="pm_pay_online"):
                pay_online()
        with col2:
            if st.button("💵 **Pay Offline**", use_container_width=True, key="pm_pay_offline"):
                pay_offline()

    if st.session_state.get("process_complete"):
        st.success("🎉 **Process Complete!**")
        st.write("Thank you for using our file transfer and print service.")
        if st.button("🔄 Start New Transfer"):
            st.session_state["process_complete"] = False
            st.session_state["payinfo"] = None
            st.session_state["status"] = ""
            st.session_state["print_ack"] = None
            st.session_state["user_id"] = str(uuid.uuid4())[:8]
            set_status("Ready for new transfer")

# Convert & Format page (unchanged)
def render_convert_page():
    st.header("📄 Convert & Format")
    st.write("Batch-convert files. Converted items appear in a separate list for previewing/printing.")

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
                    st.error(f"Failed: {uf.name}")
            if converted:
                st.session_state.converted_files_conv = converted
                st.success(f"Converted {len(converted)} files.")

    if st.session_state.converted_files_conv:
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
                    const w=window.open(url,'conv_preview_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes,toolbar=yes');
                    if(!w)alert('Allow popups to preview.');
                }})();
                </script>
                """
                components.html(js, height=0)
            if cols[2].button("Format & Print", key=f"c_format_{i}"):
                b64 = it['pdf_base64']; ts=int(time.time()*1000)
                js=f"""
                <script>
                (function(){{
                  try {{
                    const b64="{b64}";
                    const bytes=atob(b64);const arr=new Uint8Array(bytes.length);
                    for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);
                    const blob=new Blob([arr],{{type:'application/pdf'}});
                    const url=URL.createObjectURL(blob);
                    const pop = window.open(url,'conv_fprint_{ts}','width=900,height=700,scrollbars=yes,resizable=yes,menubar=yes,toolbar=yes');
                    if(pop){{ setTimeout(()=>{{ try{{ pop.print(); }}catch(e){{}} }},1200); }} else {{ alert('Allow popups for Format & Print.'); }}
                  }} catch(e){{ alert('Error'); }}
                }})();
                </script>
                """
                components.html(js, height=0)

# Main
def main():
    if page == "Print Manager":
        render_print_manager_page()
    else:
        render_convert_page()
    st.markdown("<div style='text-align:center;color:#666;padding-top:6px;'>Autoprint — Clean & Mobile-Friendly</div>", unsafe_allow_html=True)

if __name__ == "__main__":
    main()
