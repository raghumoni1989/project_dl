import os, time, subprocess, sys, uuid, io, re, asyncio, json
from datetime import datetime
from flask import Flask, render_template, request
from playwright.async_api import async_playwright
import fitz  # PyMuPDF
import pdfplumber
import mysql.connector
from mysql.connector import Error
from ftplib import FTP
from threading import Thread
import redis   # ✅ Redis for sessions

# ============================================================
# FLASK APP INIT
# ============================================================
app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================
STATIC_DIR   = "static"
CAPTCHA_DIR  = os.path.join(STATIC_DIR, "captcha")
LOCAL_BASE   = os.path.join(STATIC_DIR, "dl_uploads")
PDF_DIR      = os.path.join(LOCAL_BASE, "pdfs")
IMG_DIR      = os.path.join(LOCAL_BASE, "images")

for folder in [CAPTCHA_DIR, PDF_DIR, IMG_DIR]:
    os.makedirs(folder, exist_ok=True)

SHOW_BROWSER = False
SLOW_MO_MS   = 0

# FTP CONFIG
FTP_HOST      = "147.93.109.159"
FTP_USER      = "u949639822.raghu"
FTP_PASS      = "Laharimoniraghu@123"
FTP_BASE_PATH = "admin_dashboard/dashboard/user/dl_files"

# ============================================================
# MYSQL CONNECTION
# ============================================================
def get_db_connection():
    try:
        return mysql.connector.connect(
            host="srv1875.hstgr.io",
            user="u949639822_managecontacts",
            password="Managecontacts123",
            database="u949639822_contacts",
            port=3306,
        )
    except Error as e:
        print(f"❌ DB error: {e}")
        return None

# ============================================================
# FTP HELPER
# ============================================================
def ftp_upload(local_path, remote_path):
    try:
        ftp = FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        parts = remote_path.split("/")[:-1]
        path = ""
        for p in parts:
            if p:
                path += f"/{p}"
                try:
                    ftp.mkd(path)
                except:
                    pass
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_path}", f)
        ftp.quit()
        print(f"✅ Uploaded: {remote_path}")
        return True
    except Exception as e:
        print(f"❌ FTP upload failed: {e}")
        return False

# ============================================================
# REDIS SESSION STORE
# ============================================================
redis_client = redis.Redis(host="localhost", port=6379, db=0)

def save_session(sid, data, ttl=300):
    redis_client.setex(f"session:{sid}", ttl, json.dumps(data))

def load_session(sid):
    raw = redis_client.get(f"session:{sid}")
    return json.loads(raw) if raw else None

def delete_session(sid):
    redis_client.delete(f"session:{sid}")

# ============================================================
# PLAYWRIGHT GLOBAL INIT
# ============================================================
playwright = None
browser = None
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

async def init_playwright():
    global playwright, browser
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=not SHOW_BROWSER, slow_mo=SLOW_MO_MS)

def _loop_runner():
    _loop.run_forever()

def run_bg(coro):
    """Run async task in background"""
    return asyncio.run_coroutine_threadsafe(coro, _loop)

# Start event loop in background thread
_thread = Thread(target=_loop_runner, daemon=True)
_thread.start()
# Initialize Playwright once
run_bg(init_playwright())

# ============================================================
# PLAYWRIGHT FUNCTIONS
# ============================================================
async def start_session_async():
    sid = str(uuid.uuid4())
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    await page.goto("https://sarathi.parivahan.gov.in/sarathiservice/stateSelection.do")
    await page.wait_for_selector("#stfNameId")
    await page.select_option("#stfNameId", value="KA")
    await page.wait_for_load_state("networkidle")

    # Remove popups
    await page.evaluate("""
        let popup = document.querySelector('#updatemobileno, #contactless_statepopup');
        if (popup) popup.style.display = 'none';
        let backdrop = document.querySelector('.modal-backdrop');
        if (backdrop) backdrop.remove();
    """)

    await page.click("a:has-text('Apply for Duplicate DL')")
    await page.wait_for_load_state("networkidle")
    await page.click("input[value='Continue']")
    await page.wait_for_load_state("networkidle")

    captcha_filename = f"{sid}.png"
    captcha_path = os.path.join(CAPTCHA_DIR, captcha_filename)
    await page.locator("img#capimg").screenshot(path=captcha_path)

    # ✅ store in Redis instead of local dict
    save_session(sid, {"context": True})
    return sid, f"captcha/{captcha_filename}"

async def finish_session_async(sid, dl_number, dob, captcha_value):
    sess = load_session(sid)
    if not sess:
        raise Exception("Session expired")
    delete_session(sid)  # cleanup

    # NOTE: We cannot store Playwright objects in Redis,
    # so you need to manage them differently (like single browser per request).
    # Simplest way: always create fresh context+page here again
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    try:
        # Fill form
        await page.goto("https://sarathi.parivahan.gov.in/sarathiservice/stateSelection.do")
        await page.select_option("#stfNameId", value="KA")
        await page.click("a:has-text('Apply for Duplicate DL')")
        await page.click("input[value='Continue']")

        await page.wait_for_selector("input[name='dlno']", timeout=20000)
        await page.fill("input[name='dlno']", dl_number)
        await page.wait_for_selector("input[name='dob']", timeout=20000)
        await page.fill("input[name='dob']", dob)

        captcha_sel = None
        for sel in ["#entCaptcha", "#captcha", "input[name='captcha']",
                    "input[placeholder*='Captcha']", "input[id*='captcha']"]:
            try:
                await page.wait_for_selector(sel, timeout=3000)
                captcha_sel = sel
                break
            except:
                continue
        if not captcha_sel:
            raise Exception("Captcha input not found")
        await page.fill(captcha_sel, captcha_value)

        await page.check("input[type='checkbox']")
        await page.click("input[value='Get DL Details']")
        try:
            await page.wait_for_selector("text=Driving Licence Details", timeout=15000)
        except:
            print("[WARN] DL details page not confirmed")
        await asyncio.sleep(3)

        await page.emulate_media(media="print")
        pdf_bytes = await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        return pdf_bytes

    finally:
        try:
            await context.close()
        except:
            pass

# ============================================================
# EXTRACTION HELPERS
# ============================================================
def format_date(date_str):
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%d/%m/%Y")
    except:
        return date_str

def safe_value(label, value):
    return value.strip() if value and value.strip() else f'Not Found <button onclick="editField(\'{label}\')">Edit</button>'

def extract_details(pdf_bytes):
    details = {
        "Driving Licence Number": None, "Date of Birth": None, "Name": None,
        "Father's Name": None, "Blood Group": None, "Present Address": None,
        "State": "Karnataka", "RTO": None, "Class of Vehicles": None,
        "DOI": None, "VALID TILL": None, "Validity Period (Non-Transport)": None,
    }
    text_all = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i in range(min(2, len(pdf.pages))):
            page_text = pdf.pages[i].extract_text()
            if page_text:
                text_all += page_text + "\n"

    dl_match     = re.search(r"Driving Licence Number\s*\n?([A-Z0-9\s/-]+)", text_all)
    dob_match    = re.search(r"Date of Birth\s*\n?([0-9-]+)", text_all)
    name_match   = re.search(r"Name\s*:\s*(.*)", text_all)
    father_match = re.search(r"Father's Name\s*:\s*(.*)", text_all)
    blood_match  = re.search(r"Blood Group\s*:\s*([A-Z+]+)", text_all)
    rto_match    = re.search(r"RTO\s*[-:]?\s*([A-Za-z.,\s]+?)(?=\s*Class of Vehicles|\n)", text_all)

    details["Driving Licence Number"] = safe_value("Driving Licence Number", dl_match.group(1) if dl_match else None)
    details["Date of Birth"]          = safe_value("Date of Birth", format_date(dob_match.group(1)) if dob_match else None)
    details["Name"]                   = safe_value("Name", name_match.group(1) if name_match else None)
    details["Father's Name"]          = safe_value("Father's Name", father_match.group(1) if father_match else None)
    details["Blood Group"]            = safe_value("Blood Group", blood_match.group(1) if blood_match else None)
    details["RTO"]                    = safe_value("RTO", rto_match.group(1) if rto_match else None)

    return details

def process_pdf(pdf_bytes, upload_id):
    page_files = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    img_dir = os.path.join(IMG_DIR, upload_id)
    os.makedirs(img_dir, exist_ok=True)
    img_counter = 1
    for page_num in range(min(2, len(doc))):
        page = doc[page_num]
        for xref, *_ in page.get_images(full=True):
            base = doc.extract_image(xref)
            img_bytes = base["image"]
            w, h = base.get("width", 0), base.get("height", 0)
            if h < 40:
                continue
            fn = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_p{page_num+1}_i{img_counter}.png"
            local_path = os.path.join(img_dir, fn)
            with open(local_path, "wb") as f:
                f.write(img_bytes)
            remote_path = f"{FTP_BASE_PATH}/images/{upload_id}/{fn}"
            ftp_upload(local_path, remote_path)
            page_files.append({"filename": fn, "width": w, "height": h})
            img_counter += 1
    return page_files

# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return render_template("loading.html")

@app.route("/new_session", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        sid, captcha_file = run_bg(start_session_async()).result()
        return render_template("new_session.html", session_id=sid, captcha_image=captcha_file)
    else:
        dlno    = request.form["dl_number"]
        dob     = request.form["dob"]
        captcha = request.form["captcha"]
        sid     = request.form["session_id"]

        pdf_bytes = run_bg(finish_session_async(sid, dlno, dob, captcha)).result()
        upload_id    = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        pdf_filename = f"{upload_id}_DL_Result.pdf"
        pdf_path     = os.path.join(PDF_DIR, pdf_filename)

        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        ftp_upload(pdf_path, f"{FTP_BASE_PATH}/pdfs/{pdf_filename}")

        details     = extract_details(pdf_bytes)
        page_images = process_pdf(pdf_bytes, upload_id)

        return render_template("results.html", details=details, upload_id=upload_id, page_images=page_images)

@app.route("/save_data", methods=["POST"])
def save_data():
    data = request.form.to_dict()
    upload_id = data.get("upload_id")
    conn = get_db_connection()
    if not conn:
        return "❌ DB connection failed", 500
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT INTO dl_details (upload_id, dl_number, dob, name, father_name, blood_group, present_address, state, rto, class_of_vehicles)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            upload_id, data.get('Driving Licence Number'), data.get('Date of Birth'),
            data.get('Name'), data.get("Father's Name"), data.get('Blood Group'),
            data.get('Present Address'), data.get('State'), data.get('RTO'),
            data.get('Class of Vehicles')
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return f"❌ Error saving data: {e}", 500
    finally:
        cursor.close()
        conn.close()
    return f"✅ DL data for {upload_id} saved successfully!"

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
