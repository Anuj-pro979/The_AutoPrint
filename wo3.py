# wo3_autoprint_fixed_pages_firestore_sender_upgraded.py
# Streamlit sender that uploads chunked base64 docs + manifest to Firestore
# Upgrades: robust conversion, local payment estimation fallback, better logging/UI

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

# Firestore imports (firebase_admin)
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

# Logging
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

# Utilities
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

def retry_with_backoff(func, attempts=3, initial_delay=0.5, factor=2.0, *args, **kwargs):
    delay = initial_delay
    last_exc = None
    for i in range(attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            log(f"Attempt {i+1}/{attempts} failed for {getattr(func,'__name__',str(func))}: {e}", "warning")
            logger.debug(traceback.format_exc())
            time.sleep(delay)
            delay *= factor
    log(f"All {attempts} attempts failed for {getattr(func,'__name__',str(func))}", "error")
    if last_exc:
        raise last_exc
    return None

# Pricing defaults (match receiver DEFAULT_CONFIG)
DEFAULT_PRICING = {
    "price_bw_per_page": 2.00,
    "price_color_per_page": 5.00,
    "price_duplex_discount": 0.9,
    "min_charge": 5.00,
    "currency": "INR",
    "owner_upi": "owner@upi"
}

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

# Data classes
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

# FileConverter with improved fallbacks
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
                # naive wrapping
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
                # choose resample attribute compatible across Pillow versions
                try:
                    resample_lanczos = Image.LANCZOS
                except Exception:
                    try:
                        resample_lanczos = Image.Resampling.LANCZOS
                    except Exception:
                        resample_lanczos = Image.BICUBIC
                if img.size[0] > 2000 or img.size[1] > 2000:
                    img.thumbnail((2000, 2000), resample=resample_lanczos)
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
    def convert_uploaded_file_to_pdf_bytes(cls, uploaded_file) -> Optional[bytes]:
        """Try multiple strategies; return PDF bytes or None."""
        if not uploaded_file:
            return None
        suffix = os.path.splitext(uploaded_file.name)[1].lower()
        content = uploaded_file.getvalue()
        try:
            # If already PDF, return bytes directly
            if suffix == ".pdf":
                return content

            if suffix in cls.SUPPORTED_TEXT_EXTENSIONS:
                res = cls.convert_text_to_pdf_bytes(content)
                if res:
                    return res

            if suffix in cls.SUPPORTED_IMAGE_EXTENSIONS:
                res = cls.convert_image_to_pdf_bytes(content)
                if res:
                    return res

            # For office formats, try converting with available backends (best-effort)
            if suffix in (".docx", ".pptx"):
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                    tf.write(content)
                    tf.flush()
                    tmpname = tf.name
                try:
                    # Try docx2pdf / COM / LibreOffice as in previous code
                    # Keep this code minimal — if conversion fails, fall back to sending original bytes
                    # If docx2pdf / LibreOffice not available in Streamlit Cloud, it's okay:
                    from_path = tmpname
                    # Try LibreOffice headless if present
                    soffice = find_executable(["soffice", "libreoffice"])
                    if soffice:
                        cmd = [soffice, "--headless", "--convert-to", "pdf", "--outdir", os.path.dirname(tmpname), tmpname]
                        ok, out = run_subprocess(cmd, timeout=cls.LIBREOFFICE_TIMEOUT)
                        expected = os.path.join(os.path.dirname(tmpname), os.path.splitext(os.path.basename(tmpname))[0] + ".pdf")
                        if ok and os.path.exists(expected):
                            with open(expected, "rb") as f:
                                data = f.read()
                            safe_remove(expected)
                            return data
                    # else: no reliable conversion available — return None so caller will send original bytes
                finally:
                    safe_remove(tmpname)

            # As a last resort try pandoc if available (rare on Streamlit Cloud)
            if suffix not in (".pdf",) and suffix not in cls.SUPPORTED_IMAGE_EXTENSIONS:
                try:
                    if 'pypandoc' in globals() and pypandoc:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                            tf.write(content); tf.flush(); tmpname = tf.name
                        try:
                            pandoc_exec = find_executable(["pandoc"])
                            if pandoc_exec:
                                out_pdf = tmpname + ".pdf"
                                cmd = [pandoc_exec, tmpname, "-o", out_pdf]
                                ok, out = run_subprocess(cmd, timeout=cls.PANDOC_TIMEOUT)
                                if ok and os.path.exists(out_pdf):
                                    with open(out_pdf, "rb") as f:
                                        data = f.read()
                                    safe_remove(out_pdf)
                                    safe_remove(tmpname)
                                    return data
                        finally:
                            safe_remove(tmpname)
                except Exception:
                    pass

            # nothing else worked
            return None
        except Exception as e:
            log(f"convert_uploaded_file_to_pdf_bytes failed for {uploaded_file.name}: {e}", "error")
            logger.debug(traceback.format_exc())
            return None

# Page counting (bytes->pages)
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

# Streamlit UI setup
st.set_page_config(page_title="Autoprint", layout="wide", page_icon="🖨️", initial_sidebar_state="expanded")

st.markdown("""
    <style>
      .appview-container .main .block-container {padding-top: 10px; padding-bottom:10px;}
      .stButton>button {padding:6px 10px;}
      .stDownloadButton>button {padding:6px 10px;}
      .stProgress {height:14px;}
    </style>
    """, unsafe_allow_html=True)

st.markdown("<h1 style='text-align:center;margin:6px 0 8px 0;'>Autoprint</h1>", unsafe_allow_html=True)
st.markdown("""<div style="display:flex;align-items:center;gap:10px;padding:8px;border-radius:6px;background:#f1f7ff;">
      <strong style="font-size:13px;">Tip:</strong>
      <span style="font-size:13px;">Use Convert & Format for conversions; Print Manager for sending jobs.</span>
    </div>""", unsafe_allow_html=True)

# Session state initialization
if 'converted_files_pm' not in st.session_state:
    st.session_state.converted_files_pm = []
if 'converted_files_conv' not in st.session_state:
    st.session_state.converted_files_conv = []
if 'formatted_pdfs' not in st.session_state:
    st.session_state.formatted_pdfs = {}

# Sidebar and environment info
with st.sidebar:
    st.title("Autoprint (founder: KOTA ANUJ KUMAR)")
    page = st.radio("Page", ["Print Manager", "Convert & Format"], index=0)
    st.markdown("---")
    st.caption("Streamlit sender (Firestore chunks) — adapted for reliability.")
    with st.expander("Environment"):
        st.write(f"Platform: {platform.system()}")
        st.write("PDF reader available:", bool(PDF_READER_AVAILABLE))
        st.write("Log file:", LOGFILE)
        if st.button("Show log tail", key="show_log"):
            try:
                with open(LOGFILE, "r", encoding="utf-8") as lf:
                    st.code(lf.read()[-4000:])
            except Exception as e:
                st.error(f"Could not read log file: {e}")

# Firestore init (Streamlit secrets)
COLLECTION = "files"
CHUNK_SIZE = 200_000
FIRESTORE_OK = False
db = None

def init_firestore_from_st_secrets():
    global FIRESTORE_OK, db
    try:
        svc = st.secrets.get("firebase_service_account")
        if not svc:
            raise RuntimeError("No firebase_service_account in st.secrets")
        if isinstance(svc, dict):
            sa = svc
        else:
            sa = json.loads(svc)
        if "private_key" in sa and isinstance(sa["private_key"], str):
            sa["private_key"] = sa["private_key"].replace("\\n", "\n")
        if firebase_admin is None or credentials is None or firestore is None:
            raise RuntimeError("firebase_admin SDK not available in environment. Install firebase-admin.")
        try:
            firebase_admin.get_app()
        except ValueError:
            cred = credentials.Certificate(sa)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        FIRESTORE_OK = True
        log("Firestore initialized", "info")
    except Exception as e:
        FIRESTORE_OK = False
        db = None
        log(f"Firestore initialization failed: {e}", "error")
        logger.debug(traceback.format_exc())

init_firestore_from_st_secrets()
if not FIRESTORE_OK:
    st.warning("Firestore not initialized. Add 'firebase_service_account' to Streamlit Secrets (see sidebar).")

# Helpers for doc IDs
def meta_doc_id(file_id: str) -> str:
    return f"{file_id}_meta"

def chunk_doc_id(file_id: str, idx: int) -> str:
    return f"{file_id}_{idx}"

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

# Session keys related to sending/paying
for key, default in {
    "payinfo": None,
    "status": "",
    "process_complete": False,
    "user_name": "",
    "user_id": str(uuid.uuid4())[:8],
    "print_ack": None,
    "pricing": DEFAULT_PRICING
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

def set_status(s):
    st.session_state["status"] = f"{datetime.datetime.now().strftime('%H:%M:%S')} - {s}"

def generate_upi_uri(upi_id, amount, note=None):
    params = [f"pa={upi_id}", f"am={amount}"]
    if note:
        params.append(f"tn={note}")
    return "upi://pay?" + "&".join(params)

# Core: Firestore uploader with improved behavior
def send_multiple_files(converted_files: List[ConvertedFile], copies: int, color_mode: str):
    """
    Upload files as base64 chunks to Firestore and write manifests.
    Wait for receiver 'payinfo' but after short wait show a locally computed estimate so payment UI appears quickly.
    """
    global db, FIRESTORE_OK
    if not FIRESTORE_OK or db is None:
        st.error("Firestore not initialized. Cannot send files.")
        return

    if not converted_files:
        st.error("No files to send.")
        return

    set_status("Preparing upload...")
    try:
        job_id = str(uuid.uuid4())[:12]
        files_meta = []
        total_bytes = 0

        for cf in converted_files:
            blob = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
            size = len(blob)
            pages = count_pdf_pages(blob)
            fid = str(uuid.uuid4())[:8]
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
                "blob": blob
            })
            total_bytes += size

        # Upload chunks
        set_status("Uploading chunks to Firestore...")
        progress_bar = st.progress(0.0)
        total_chunks_all = 0
        for m in files_meta:
            b64 = base64.b64encode(m["blob"]).decode("utf-8")
            parts = [b64[i:i+CHUNK_SIZE] for i in range(0, len(b64), CHUNK_SIZE)]
            m["parts"] = parts
            m["total_chunks"] = len(parts)
            m["sha256"] = sha256_bytes(m["blob"])
            total_chunks_all += len(parts)

        uploaded = 0
        for m in files_meta:
            fid = m["file_id"]
            for idx, piece in enumerate(m["parts"]):
                doc_ref = db.collection(COLLECTION).document(chunk_doc_id(fid, idx))
                # write chunk doc (retry)
                retry_with_backoff(lambda d=doc_ref, p=piece, i=idx: d.set({"data": p, "chunk_index": i}), attempts=3)
                uploaded += 1
                try:
                    progress_bar.progress(min(1.0, uploaded / max(1, total_chunks_all)))
                except Exception:
                    pass

        set_status(f"Uploaded {uploaded} chunks. Writing manifests...")

        # Write manifests
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
            # write manifest reliably
            def _write_meta(mref=db.collection(COLLECTION).document(meta_doc_id(fid)), data=meta_doc):
                mref.set(data, merge=True)
            retry_with_backoff(_write_meta, attempts=3)
            set_status(f"Manifest written for {m['filename']} (id={fid})")

        # After writing manifests, poll for payinfo — but show local estimate promptly after a short wait
        set_status("Waiting for receiver to add payinfo (short wait before showing estimate)...")
        local_estimate_shown = False
        payinfo_collected = {}
        poll_start = time.time()
        short_wait = 8  # seconds before showing local estimate
        total_poll = 90  # total seconds to poll for real payinfo
        while time.time() - poll_start < total_poll:
            # poll each manifest
            for m in files_meta:
                fid = m["file_id"]
                try:
                    snap = db.collection(COLLECTION).document(meta_doc_id(fid)).get()
                    if snap.exists:
                        md = snap.to_dict() or {}
                        if "payinfo" in md and md["payinfo"]:
                            payinfo_collected[fid] = md["payinfo"]
                except Exception:
                    logger.debug(traceback.format_exc())
            # if we have at least one payinfo, use it (first)
            if payinfo_collected:
                first = next(iter(payinfo_collected.items()))
                st.session_state["payinfo"] = first[1]
                set_status("Payment information received from receiver.")
                break
            # if short wait passed and we haven't shown estimate yet -> compute local estimate and show
            if not local_estimate_shown and time.time() - poll_start >= short_wait:
                # compute estimate for entire job
                # use current st.session_state.pricing (owner can override in UI)
                cfg = st.session_state.get("pricing", DEFAULT_PRICING)
                # choose color as True if any file has colorMode containing 'color'
                total_amount = 0.0
                summary = []
                for m in files_meta:
                    is_color = ("color" in str(m.get("settings", {}).get("colorMode", "")).lower()) or (color_mode and "color" in color_mode.lower())
                    duplex_flag = False
                    d = m.get("settings", {}).get("duplex", "") or ""
                    if d and ("two" in str(d).lower() or "duplex" in str(d).lower()):
                        duplex_flag = True
                    amt = calculate_amount(cfg, m["pages"], copies=int(m["settings"].get("copies", 1)), color=is_color, duplex=duplex_flag)
                    summary.append({"file_name": m["filename"], "pages": m["pages"], "amount": amt})
                    total_amount += amt
                est_payinfo = {
                    "order_id": job_id,
                    "file_name": "Multiple" if len(summary) > 1 else summary[0]["file_name"],
                    "pages": sum(m["pages"] for m in files_meta),
                    "copies": copies,
                    "amount": round(total_amount, 2),
                    "amount_str": f"{round(total_amount, 2):.2f}",
                    "currency": cfg.get("currency", "INR"),
                    "owner_upi": cfg.get("owner_upi", DEFAULT_PRICING["owner_upi"]),
                    "upi_url": f"upi://pay?pa={cfg.get('owner_upi')}&pn=PrintService&am={round(total_amount,2):.2f}&cu={cfg.get('currency','INR')}",
                    "status": "estimated",
                    "estimated": True,
                }
                st.session_state["payinfo"] = est_payinfo
                local_estimate_shown = True
                set_status("Showing local payment estimate while waiting for official payinfo.")
                # keep polling but break loop only if official payinfo arrives
            time.sleep(1.0)

        # after polling loop
        if not st.session_state.get("payinfo"):
            set_status("No payinfo received; showing local estimate (if available)")
        st.success(f"Upload complete. Job id: {job_id}")
        return

    except Exception as e:
        log(f"Upload failed: {e}", "error")
        logger.debug(traceback.format_exc())
        st.error(f"Upload failed: {e}")
        set_status("Upload failed")
        return

# Payment helpers
def pay_offline():
    payinfo = st.session_state.get("payinfo", {}) or {}
    amount = payinfo.get("amount", 0)
    currency = payinfo.get("currency", "INR")
    st.success(f"💵 Please pay ₹{amount} {currency} offline")
    st.balloons()
    st.session_state["payinfo"] = None
    st.session_state["process_complete"] = True

def pay_online():
    payinfo = st.session_state.get("payinfo", {}) or {}
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
    st.session_state["payinfo"] = None
    st.session_state["process_complete"] = True

def cancel_payment():
    st.session_state["payinfo"] = None
    set_status("Cancelled by user")

# Render UI pages
def render_print_manager_page():
    st.header("📄 File Transfer & Print Service (Firestore)")
    st.write("Upload files (multiple). Convert & send as a print job. Receiver will enqueue & reply with payinfo.")

    st.markdown("### 👤 User Info")
    user_name = st.text_input("Your name (optional)", value=st.session_state.get("user_name", ""), key="ui_user_name")
    st.session_state["user_name"] = user_name
    st.caption(f"Your ID: {st.session_state['user_id']}")

    uploaded = st.file_uploader("Upload files (multiple)", accept_multiple_files=True,
                                type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'],
                                key="pm_multi_upload")
    if uploaded:
        with st.spinner("Converting uploaded files..."):
            conv_list = st.session_state.get("converted_files_pm", [])
            added = 0
            for uf in uploaded:
                if any(x.orig_name == uf.name for x in conv_list):
                    continue
                try:
                    original_bytes = uf.getvalue()
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                    if pdf_bytes:
                        cf = ConvertedFile(orig_name=uf.name, pdf_name=os.path.splitext(uf.name)[0] + ".pdf", pdf_bytes=pdf_bytes, settings=PrintSettings(), original_bytes=original_bytes)
                    else:
                        # conversion failed — keep original bytes to send as fallback
                        cf = ConvertedFile(orig_name=uf.name, pdf_name=uf.name, pdf_bytes=b"", settings=PrintSettings(), original_bytes=original_bytes)
                        st.warning(f"Conversion to PDF was not possible for {uf.name}. Original bytes will be sent.")
                    conv_list.append(cf)
                    added += 1
                except Exception as e:
                    log(f"Conversion failed for {uf.name}: {e}", "warning")
            st.session_state.converted_files_pm = conv_list
            if added:
                st.success(f"Added {added} file(s).")

    st.subheader("📂 Files in queue")
    conv = st.session_state.get("converted_files_pm", [])
    if not conv:
        st.info("No files in queue.")
    else:
        for idx, cf in enumerate(conv):
            cols = st.columns([4,1,1,1])
            with cols[0]:
                checked_key = f"sel_file_{idx}"
                if checked_key not in st.session_state:
                    st.session_state[checked_key] = True
                st.checkbox(f"{cf.pdf_name} (orig: {cf.orig_name})", value=st.session_state[checked_key], key=checked_key)
                if st.button(f"Preview {idx}", key=f"preview_pm_{idx}"):
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
                            const w=window.open(url,'pm_preview_{ts}','width=900,height=700');
                            if(!w)alert('Allow popups to preview.');
                        }})();
                        </script>
                        """
                        components.html(js, height=0)
                    else:
                        st.warning("No preview available.")
            with cols[1]:
                if st.button("Download", key=f"dl_pm_{idx}"):
                    data = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b"")
                    st.download_button("Download", data=data, file_name=cf.pdf_name, mime="application/pdf" if cf.pdf_bytes else "application/octet-stream", key=f"dlpm_{idx}")
            with cols[2]:
                if st.button("Remove", key=f"rm_pm_{idx}"):
                    new_list = [x for x in st.session_state.converted_files_pm if x.orig_name != cf.orig_name]
                    st.session_state.converted_files_pm = new_list
                    set_status(f"Removed {cf.orig_name}")
            with cols[3]:
                blob_for_count = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b'')
                pages = count_pdf_pages(blob_for_count)
                st.caption(f"{pages}p")

        selected_files = [cf for idx, cf in enumerate(conv) if st.session_state.get(f"sel_file_{idx}", True)]

        st.markdown("---")
        st.markdown("### 🖨️ Job Settings")
        col1, col2 = st.columns(2)
        with col1:
            copies = st.number_input("Copies per file", min_value=1, max_value=10, value=1, key="pm_job_copies")
        with col2:
            color_mode = st.selectbox("Color mode", options=["Auto", "Color", "Monochrome"], key="pm_job_colormode")

        if st.button("📤 Send Selected Files", key="pm_send_multi"):
            if not selected_files:
                st.error("No files selected.")
            else:
                # upload & wait (blocking)
                send_multiple_files(selected_files, copies, color_mode)

    # status & payment UI
    if st.session_state.get("status"):
        st.info(f"📊 Status: {st.session_state['status']}")

    if st.session_state.get("print_ack"):
        ack = st.session_state["print_ack"]
        st.success(f"🖨️ Print result: {ack.get('status')} — {ack.get('note','')}")

    payinfo = st.session_state.get("payinfo")
    if payinfo and not st.session_state.get("process_complete"):
        st.markdown("---")
        st.markdown("## 💳 Payment")
        col1, col2 = st.columns(2)
        with col1:
            st.write(f"**File:** {payinfo.get('file_name', 'Multiple')}")
            st.write(f"**Amount:** ₹{payinfo.get('amount', 0)} {payinfo.get('currency', 'INR')}")
            st.write(f"**Pages:** {payinfo.get('pages', 'N/A')}")
        with col2:
            st.write(f"**Copies:** {payinfo.get('copies', 1)}")
            if payinfo.get("estimated"):
                st.warning("This is a local estimate. Waiting for official payinfo from receiver...")
                if st.button("Use estimated payment now", key="use_estimate"):
                    pay_online()
            else:
                if st.button("Pay Online", key="pm_pay_online"):
                    pay_online()
                if st.button("Pay Offline", key="pm_pay_offline"):
                    pay_offline()

    if st.session_state.get("process_complete"):
        st.success("🎉 Process Complete")
        if st.button("Start New Transfer"):
            st.session_state["process_complete"] = False
            st.session_state["payinfo"] = None
            st.session_state["status"] = ""
            st.session_state["print_ack"] = None
            st.session_state["user_id"] = str(uuid.uuid4())[:8]
            set_status("Ready")

def render_convert_page():
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
                    const w=window.open(url,'conv_preview_{ts}','width=900,height=700');
                    if(!w)alert('Allow popups to preview.');
                }})();
                </script>
                """
                components.html(js, height=0)
            if cols[2].button("Add to Print Queue", key=f"c_add_{i}"):
                # turn into ConvertedFile and add to queue
                cf = ConvertedFile(orig_name=it['orig_name'], pdf_name=it['pdf_name'], pdf_bytes=it['pdf_bytes'], settings=PrintSettings(), original_bytes=None)
                lst = st.session_state.get("converted_files_pm", [])
                lst.append(cf)
                st.session_state.converted_files_pm = lst
                st.success("Added to print queue.")

def main():
    if page == "Print Manager":
        render_print_manager_page()
    else:
        render_convert_page()
    st.markdown("<div style='text-align:center;color:#666;padding-top:6px;'>Autoprint — Firestore chunked upload sender (upgraded)</div>", unsafe_allow_html=True)

if __name__ == "__main__":
    main()
