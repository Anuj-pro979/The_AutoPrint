# wo3_autoprint_streamlit_full_upgraded.py
# Upgraded Autoprint Streamlit app
# - Uses wo3_autoprint_fixed_pages.FileConverter when available
# - Safe temp cleanup, environment diagnostics
# - Chunked Firestore upload, job_id polling for payinfo
# Run: streamlit run wo3_autoprint_streamlit_full_upgraded.py

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
import io

# Firestore (soft import)
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

# Optional QR
try:
    import qrcode
    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False

# Try to import your conversion module
try:
    from wo3_autoprint_fixed_pages import FileConverter, ConvertedFile, PrintSettings, count_pdf_pages
    USING_USER_MODULE = True
except Exception:
    USING_USER_MODULE = False
    # Minimal fallback classes/functions so app doesn't crash
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

    class FileConverter:
        SUPPORTED_TEXT_EXTENSIONS = {'.txt', '.md', '.rtf', '.html', '.htm'}
        SUPPORTED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp'}

        @classmethod
        def convert_text_to_pdf_bytes(cls, file_content: bytes, encoding='utf-8') -> Optional[bytes]:
            try:
                text = file_content.decode(encoding, errors='ignore')
                pdf = FPDF(unit='mm', format='A4')
                pdf.set_auto_page_break(auto=True, margin=15)
                pdf.add_page()
                pdf.set_font("Helvetica", size=11)
                for line in text.splitlines():
                    pdf.multi_cell(0, 6, line)
                return pdf.output(dest='S').encode('latin-1', errors='replace')
            except Exception:
                return None

        @classmethod
        def convert_image_to_pdf_bytes(cls, file_content: bytes) -> Optional[bytes]:
            try:
                from io import BytesIO
                with Image.open(io.BytesIO(file_content)) as img:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    out = io.BytesIO()
                    img.thumbnail((2000, 2000))
                    img.save(out, format='PDF')
                    return out.getvalue()
            except Exception:
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
            except Exception:
                return None
            return None

    def count_pdf_pages(blob: Optional[bytes]) -> int:
        # minimal fallback
        return 1

# -------- logging with safe logfile creation ----------
def make_logfile():
    # try tmp, else fallback to ~/.cache/autoprint
    tdir = tempfile.gettempdir()
    try:
        os.makedirs(tdir, exist_ok=True)
        path = os.path.join(tdir, f"autoprint_sender_{int(time.time())}.log")
        fh = logging.FileHandler(path, encoding="utf-8")
        return path, fh
    except Exception:
        alt_dir = os.path.expanduser("~/.cache/autoprint")
        os.makedirs(alt_dir, exist_ok=True)
        path = os.path.join(alt_dir, f"autoprint_sender_{int(time.time())}.log")
        fh = logging.FileHandler(path, encoding="utf-8")
        return path, fh

LOGFILE, fh = make_logfile()
logger = logging.getLogger("autoprint_sender_full")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
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

# -------- utilities ----------
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

# --------- cleanup helpers ----------
APP_TMP_PREFIXES = ("autoprint_sender_", "autoprint_", "docx_out_", "pptx_out_", "generic_out_")
AUTO_CLEAN_AGE_HOURS = 72  # auto-clean files older than this at startup

def cleanup_old_autoprint_temp(tmpdir=None, prefixes=APP_TMP_PREFIXES, age_hours=AUTO_CLEAN_AGE_HOURS, dry_run=True):
    tmpdir = tmpdir or tempfile.gettempdir()
    now = time.time()
    inspected = []
    actions = []
    try:
        for name in os.listdir(tmpdir):
            for pref in prefixes:
                if name.startswith(pref):
                    path = os.path.join(tmpdir, name)
                    try:
                        st_mode = os.stat(path)
                        age = (now - st_mode.st_mtime) / 3600.0
                        inspected.append((path, round(age, 2)))
                        if age >= age_hours:
                            if dry_run:
                                actions.append(("DRYRUN", path))
                            else:
                                try:
                                    if os.path.isdir(path):
                                        shutil.rmtree(path)
                                    else:
                                        os.remove(path)
                                    actions.append(("REMOVED", path))
                                except Exception as e:
                                    actions.append(("ERR", path, str(e)))
                    except Exception as e:
                        actions.append(("STAT_ERR", path, str(e)))
    except Exception as e:
        logger.debug("cleanup_old_autoprint_temp failed:\n" + traceback.format_exc())
    return {"inspected": inspected, "actions": actions}

# perform a conservative auto-clean at startup (dry-run + actual for very old files)
try:
    debug_cleanup = cleanup_old_autoprint_temp(dry_run=True)
    # automatically remove extremely old files (age >= 7 days) to avoid temp folder filling up
    extreme_cleanup = cleanup_old_autoprint_temp(age_hours=24*7, dry_run=False)
    log(f"Startup temp scan: inspected {len(debug_cleanup['inspected'])}, planned actions {len(debug_cleanup['actions'])}")
    log(f"Startup extreme cleanup executed: actions {len(extreme_cleanup['actions'])}")
except Exception:
    logger.debug(traceback.format_exc())

# --------- Firestore & constants ----------
COLLECTION = "files"
CHUNK_SIZE = 200_000  # characters per base64 chunk (safe for Firestore)
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
        st.session_state.status = "Firestore initialized"
        log("Firestore initialized", "info")
    except Exception as e:
        FIRESTORE_OK = False
        db = None
        FIRESTORE_ERR = str(e)
        st.session_state.status = f"Firestore init failed: {e}"
        log(f"Firestore init failed: {e}", "error")
        logger.debug(traceback.format_exc())

# Try to init Firestore (will set FIRESTORE_OK appropriately)
init_firestore_from_secrets()

# session state init
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
    log(st.session_state.status, "info")

# helpers
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def meta_doc_id(file_id: str) -> str:
    return f"{file_id}_meta"

def chunk_doc_id(file_id: str, idx: int) -> str:
    return f"{file_id}_{idx}"

# price helpers
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

# send_multiple_files with robust job_id polling (uses Firestore)
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
            b64 = base64.b64encode(blob).decode("utf-8") if blob else ""
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

        for m in files_meta:
            fid = m["file_id"]
            meta_doc = {
                "total_chunks": int(m["total_chunks"]),
                "file_name": m["filename"],
                "orig_filename": m.get("orig_filename", ""),
                "sha256": m["sha256"],
                "settings": m.get("settings", {}),
                "user_name": st.session_state.get("user_name") or "",
                "user_id": st.session_state.get("user_id"),
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "job_id": job_id,
                "file_size_bytes": int(m.get("size_bytes", 0)),
                "status": "uploaded",
                "waiting_for_payinfo": True
            }
            def _write_meta(mref=db.collection(COLLECTION).document(meta_doc_id(fid)), data=meta_doc):
                mref.set(data, merge=True)
            retry_with_backoff(_write_meta, attempts=3)
            set_status(f"Wrote manifest for {m['filename']} (id={fid})")

        # Poll for payinfo — query manifests by job_id (more robust)
        set_status("Waiting for receiver to write payinfo into any manifest (polling by job_id)...")
        st.session_state.payinfo = None
        poll_start = time.time()
        short_wait = 6
        total_poll = 90  # seconds
        local_estimate_shown = False

        # helper: check payinfo by job_id
        def _check_payinfo_by_job():
            try:
                q = db.collection(COLLECTION).where("job_id", "==", job_id).limit(50)
                snaps = q.get()
                for s in snaps:
                    try:
                        md = s.to_dict() or {}
                        # tolerant keys
                        for key in ("payinfo","payment","payment_info","paymentInfo"):
                            if md.get(key):
                                return md.get(key)
                    except Exception:
                        logger.debug(traceback.format_exc())
                        continue
            except Exception:
                logger.debug("Error querying payinfo by job_id:\n" + traceback.format_exc())
            return None

        while time.time() - poll_start < total_poll:
            found_payinfo = _check_payinfo_by_job()
            if found_payinfo:
                st.session_state.payinfo = found_payinfo
                set_status("Received payment info from receiver.")
                break

            if not local_estimate_shown and (time.time() - poll_start) >= short_wait:
                cfg = st.session_state.get("pricing") or {}
                total_amount = 0.0
                for m in files_meta:
                    is_color = ("color" in str(m.get("settings", {}).get("colorMode", "")).lower()) or ("color" in str(color_mode).lower())
                    duplex_flag = False
                    d = m.get("settings", {}).get("duplex", "") or ""
                    if d and ("two" in str(d).lower() or "duplex" in str(d).lower() or "double" in str(d).lower()):
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
            if not local_estimate_shown:
                cfg = st.session_state.get("pricing") or {}
                total_amount = 0.0
                for m in files_meta:
                    is_color = ("color" in str(m.get("settings", {}).get("colorMode", "")).lower()) or ("color" in str(color_mode).lower())
                    duplex_flag = False
                    d = m.get("settings", {}).get("duplex", "") or ""
                    if d and ("two" in str(d).lower() or "duplex" in str(d).lower() or "double" in str(d).lower()):
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

        st.success(f"Upload complete. Job id: {job_id}")
        return

    except Exception as e:
        logger.debug(traceback.format_exc())
        st.error(f"Upload failed: {e}")
        set_status("Upload failed")
        return

# payment flows
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
    amount = payinfo.get("amount", 0)
    file_name = payinfo.get("file_name", "Print Job")
    # prefer owner_upi from payinfo else pricing
    if not owner_upi:
        owner_upi = st.session_state.get("pricing", {}).get("owner_upi")
    if not owner_upi:
        st.error("Payment information not available")
        return
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
    st.info("Complete the payment in your payment app.")
    st.balloons()
    st.session_state.payinfo = None
    st.session_state.process_complete = True

def cancel_payment():
    st.session_state.payinfo = None
    set_status("Cancelled by user")

# --------- Streamlit UI ----------
st.set_page_config(page_title="Autoprint (Upgraded)", layout="wide")
st.markdown("<h1 style='text-align:center;margin:6px 0 8px 0;'>Autoprint — Firestore Sender (Upgraded)</h1>", unsafe_allow_html=True)

# sidebar diagnostics & environment
with st.sidebar.expander("Environment & Diagnostics"):
    st.write(f"Platform: {platform.system()}")
    st.write("Using user conversion module:", USING_USER_MODULE)
    st.write("LibreOffice on PATH:", bool(find_executable(["soffice", "libreoffice"])))
    st.write("Log file:", LOGFILE)
    if st.button("Show log tail"):
        try:
            with open(LOGFILE, "r", encoding="utf-8") as lf:
                st.code(lf.read()[-4000:])
        except Exception as e:
            st.error(f"Could not read log file: {e}")

    if st.button("Run quick env diagnostic"):
        import shutil, importlib, sys
        diag = {}
        for exe in ("soffice","libreoffice","pandoc","pdflatex"):
            diag[exe] = shutil.which(exe)
        pkgs = ["python-docx","docx2pdf","python-pptx","pypandoc","PyPDF2","fpdf","qrcode"]
        pkg_info = {}
        for p in pkgs:
            try:
                m = importlib.import_module(p.replace("-", "_"))
                pkg_info[p] = getattr(m, "__version__", "ok")
            except Exception as e:
                pkg_info[p] = f"missing ({e})"
        st.write("Executables:", diag)
        st.write("Python packages:", pkg_info)

    if st.button("Show /tmp space (quick)"):
        try:
            out = subprocess.check_output(["df", "-h", tempfile.gettempdir()]).decode()
            st.code(out)
            out2 = subprocess.check_output("du -sh {}/* 2>/dev/null | sort -rh | head -n 20".format(tempfile.gettempdir()), shell=True).decode()
            st.code(out2)
        except Exception as e:
            st.error(f"Diagnostic failed: {e}")

    # cleanup controls
    st.markdown("### Temp cleanup")
    if st.button("Dry-run cleanup (show app temp files)"):
        res = cleanup_old_autoprint_temp(dry_run=True)
        st.write("Inspected:", len(res["inspected"]))
        st.write("Some actions (dryrun):", res["actions"][:40])
    if st.button("Run cleanup (remove old app temp files)"):
        res = cleanup_old_autoprint_temp(dry_run=False)
        st.write("Removed / errors:", res["actions"][:40])

# main UI: user + upload
st.markdown("## 👤 User")
user_name = st.text_input("Your name (optional)", value=st.session_state.get("user_name", ""))
if user_name != st.session_state.get("user_name", ""):
    st.session_state.user_name = user_name
st.caption(f"Your ID: {st.session_state['user_id']}")

st.markdown("## 📄 Upload & Queue")
uploaded = st.file_uploader("Upload files (multiple)", accept_multiple_files=True,
                            type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'],
                            key="pm_multi_upload_full")

if uploaded:
    with st.spinner("Converting uploads..."):
        conv_list = st.session_state.get("converted_files_pm", [])
        added = 0
        for uf in uploaded:
            if any(x.orig_name == uf.name for x in conv_list):
                continue
            try:
                original_bytes = uf.getvalue()
                pdf_bytes = None
                try:
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                except Exception:
                    logger.debug(traceback.format_exc())
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
                    st.warning(f"Conversion to PDF unavailable for {uf.name} — will send original bytes as fallback.")
                conv_list.append(cf)
                added += 1
            except Exception as e:
                log(f"Conversion error for {uf.name}: {e}", "warning")
        st.session_state.converted_files_pm = conv_list
        if added:
            st.success(f"Added {added} file(s).")

# show queue
st.markdown("### 📂 Files in queue")
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
            if st.button(f"Preview {idx}", key=f"preview_full_{idx}"):
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
                    st.warning("No preview available (no PDF bytes).")
        with cols[1]:
            if cf.pdf_bytes:
                st.download_button("Download PDF", data=cf.pdf_bytes, file_name=cf.pdf_name, mime="application/pdf", key=f"dlpdf_{idx}")
            else:
                st.download_button("Download original", data=cf.original_bytes or b"", file_name=cf.orig_name, mime="application/octet-stream", key=f"dlorig_{idx}")
        with cols[2]:
            if st.button("Remove", key=f"rm_full_{idx}"):
                st.session_state.converted_files_pm = [x for x in st.session_state.converted_files_pm if x.orig_name != cf.orig_name]
                set_status(f"Removed {cf.orig_name}")
        with cols[3]:
            blob_for_count = cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b'')
            pages = count_pdf_pages(blob_for_count)
            st.caption(f"{pages}p")

# job settings + send
selected_files = [cf for i,cf in enumerate(conv) if st.session_state.get(f"sel_file_{i}", True)]
st.markdown("---")
st.markdown("### 🖨️ Job Settings")
col1, col2 = st.columns(2)
with col1:
    copies = st.number_input("Copies per file", min_value=1, max_value=10, value=1, key="full_copies")
with col2:
    color_mode = st.selectbox("Color mode", options=["Auto", "Color", "Monochrome"], key="full_colormode")

if st.button("📤 Send Selected Files (Firestore)", key="full_send"):
    if not selected_files:
        st.error("No files selected.")
    else:
        # perform send in thread to avoid blocking UI (conversion is already done)
        import threading
        thr = threading.Thread(target=send_multiple_files, args=(selected_files, copies, color_mode), daemon=True)
        thr.start()
        st.info("Upload started in background. Watch status and Payment section.")

# status & payment view
if st.session_state.get("status"):
    st.info(f"📊 Status: {st.session_state['status']}")

payinfo = st.session_state.get("payinfo")
if payinfo and not st.session_state.get("process_complete"):
    st.markdown("---")
    st.markdown("## 💳 Payment")
    c1, c2 = st.columns(2)
    with c1:
        st.write(f"**File(s):** {payinfo.get('file_name', payinfo.get('filename', 'Multiple'))}")
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

st.markdown("<div style='text-align:center;color:#666;padding-top:6px;'>Autoprint — Upgraded sender (cleanup + robust payinfo polling)</div>", unsafe_allow_html=True)
