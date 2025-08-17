# wo3_autoprint_streamlit_firestore_sender_upgraded.py
# Streamlit sender (upgraded) — uses wo3_autoprint_fixed_pages.FileConverter
# Run: streamlit run wo3_autoprint_streamlit_firestore_sender_upgraded.py

import streamlit as st
import os
import tempfile
import base64
import time
import json
import logging
import traceback
import uuid
import datetime
from typing import List, Optional
import hashlib

# Firestore
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

# import conversion utilities from the module you supplied
try:
    from wo3_autoprint_fixed_pages import FileConverter, ConvertedFile, PrintSettings, count_pdf_pages  # user module
except Exception:
    # fallback minimal local definitions if import fails (to avoid crash while debugging)
    FileConverter = None
    ConvertedFile = None
    PrintSettings = None
    def count_pdf_pages(blob: Optional[bytes]) -> int:
        return 1

# logging
LOGFILE = os.path.join(tempfile.gettempdir(), f"autoprint_sender_upgraded_{int(time.time())}.log")
logger = logging.getLogger("autoprint_sender_upgraded")
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

# helpers
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def meta_doc_id(file_id: str) -> str:
    return f"{file_id}_meta"

def chunk_doc_id(file_id: str, idx: int) -> str:
    return f"{file_id}_{idx}"

CHUNK_SIZE = 200_000  # characters per base64 chunk (safe for Firestore doc size)

# Streamlit setup (minimal UI around upload/send)
st.set_page_config(page_title="Autoprint (Firestore upgraded)", layout="wide", page_icon="🖨️")
st.title("Autoprint — Firestore Sender (Upgraded)")

if 'converted_files_pm' not in st.session_state:
    st.session_state.converted_files_pm = []
if 'status' not in st.session_state:
    st.session_state.status = ""
if 'payinfo' not in st.session_state:
    st.session_state.payinfo = None
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

# Firestore init using st.secrets
COLLECTION = "files"
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

# retry helper (simple)
def retry_with_backoff_simple(func, attempts=3, initial_delay=0.5, factor=2.0):
    delay = initial_delay
    last_exc = None
    for i in range(attempts):
        try:
            return func()
        except Exception as e:
            last_exc = e
            log(f"Attempt {i+1}/{attempts} failed: {e}", "warning")
            logger.debug(traceback.format_exc())
            time.sleep(delay)
            delay *= factor
    if last_exc:
        raise last_exc
    return None

# price calc (same as before)
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

# send_multiple_files: upgraded polling logic (query by job_id)
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
                retry_with_backoff_simple(_write_chunk, attempts=3)
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
            retry_with_backoff_simple(_write_meta, attempts=3)
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
                # Query any manifest doc with this job_id and a non-empty payinfo field
                q = db.collection(COLLECTION).where("job_id", "==", job_id).limit(20)
                snaps = q.get()
                for s in snaps:
                    try:
                        md = s.to_dict() or {}
                        # if receiver writes payinfo under 'payinfo' key
                        if md.get("payinfo"):
                            return md.get("payinfo")
                        # some receivers might write 'payment' or 'payment_info' - try some tolerant checks
                        if md.get("payment"):
                            return md.get("payment")
                        if md.get("payment_info"):
                            return md.get("payment_info")
                    except Exception:
                        logger.debug(traceback.format_exc())
                        continue
            except Exception as e:
                logger.debug("Error querying payinfo by job_id:\n" + traceback.format_exc())
            return None

        while time.time() - poll_start < total_poll:
            found_payinfo = _check_payinfo_by_job()
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
            # if local_estimate_shown remains False, compute a final estimate now
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

# payment helpers (unchanged-ish)
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
    # Avoid server-side webbrowser.open here (not reliable on hosted servers)
    st.info("Complete the payment in your payment app.")
    st.balloons()
    st.session_state.payinfo = None
    st.session_state.process_complete = True

def cancel_payment():
    st.session_state.payinfo = None
    set_status("Cancelled by user")

# Minimal UI for file upload and send
st.subheader("Upload files (conversion is handled by the module you provided)")
uploaded = st.file_uploader("Upload files (multiple)", accept_multiple_files=True,
                            type=['pdf','txt','md','rtf','html','htm','png','jpg','jpeg','bmp','tiff','webp','docx','pptx'],
                            key="pm_multi_upload_upgraded")

if uploaded:
    with st.spinner("Converting..."):
        conv_list = st.session_state.get("converted_files_pm", [])
        added = 0
        for uf in uploaded:
            if any(x.orig_name == uf.name for x in conv_list):
                continue
            try:
                original_bytes = uf.getvalue()
                if FileConverter:
                    pdf_bytes = FileConverter.convert_uploaded_file_to_pdf_bytes(uf)
                else:
                    pdf_bytes = None
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

st.write("Files queued:")
conv = st.session_state.get("converted_files_pm", [])
for idx, cf in enumerate(conv):
    cols = st.columns([4,1,1,1])
    cols[0].write(f"{cf.pdf_name} (orig: {cf.orig_name})")
    cols[1].write(f"{count_pdf_pages(cf.pdf_bytes if cf.pdf_bytes else (cf.original_bytes or b''))}p")
    if cols[2].button("Remove", key=f"rm_upgraded_{idx}"):
        st.session_state.converted_files_pm = [x for i,x in enumerate(conv) if i != idx]
        set_status(f"Removed {cf.orig_name}")

if conv:
    copies = st.number_input("Copies per file", min_value=1, max_value=10, value=1, key="upg_copies")
    color_mode = st.selectbox("Color mode", options=["Auto", "Color", "Monochrome"], key="upg_colormode")
    if st.button("Send Selected Files (Firestore)", key="upg_send"):
        # send all queued files for brevity
        send_multiple_files(conv, copies, color_mode)

if st.session_state.get("status"):
    st.info(f"Status: {st.session_state['status']}")

if st.session_state.get("payinfo") and not st.session_state.get("process_complete"):
    st.markdown("---")
    st.markdown("### Payment")
    st.write(st.session_state.get("payinfo"))
    if st.button("Pay Online (Open UPI link)"):
        pay_online()
    if st.button("Mark Paid Offline"):
        pay_offline()

if st.session_state.get("process_complete"):
    st.success("Process complete.")
    if st.button("Start new"):
        st.session_state.process_complete = False
        st.session_state.payinfo = None
        st.session_state.status = ""
        st.session_state.user_id = str(uuid.uuid4())[:8]
        set_status("Ready")
