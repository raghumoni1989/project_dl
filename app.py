from flask import Flask, render_template, request
from playwright.sync_api import sync_playwright
import os, time, subprocess, sys, uuid, io, re
from datetime import datetime
import fitz  # PyMuPDF
import pdfplumber
import mysql.connector
from mysql.connector import Error
from ftplib import FTP

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

SHOW_BROWSER = False    # show browser for debugging
SLOW_MO_MS   = 0    # slow motion delay for steps

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
    """Upload a file to FTP and ensure directories exist"""
    try:
        ftp = FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)

        # Ensure path exists
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
# PLAYWRIGHT AUTOMATION
# ============================================================
def ensure_playwright_installed():
    ms_path = os.path.expanduser("~\\AppData\\Local\\ms-playwright")
    if not os.path.exists(ms_path) or not os.listdir(ms_path):
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)

sessions = {}

def start_session():
    """Start Playwright browser, navigate to DL form, capture captcha"""
    sid = str(uuid.uuid4())
    p = sync_playwright().start()

    browser = p.chromium.launch(headless=not SHOW_BROWSER, slow_mo=SLOW_MO_MS)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    # Navigate to state selection
    page.goto("https://sarathi.parivahan.gov.in/sarathiservice/stateSelection.do")
    page.wait_for_selector("#stfNameId")
    page.select_option("#stfNameId", value="KA")
    page.wait_for_load_state("networkidle")

    # Remove unwanted popups
    page.evaluate("""
        let popup = document.querySelector('#updatemobileno, #contactless_statepopup');
        if (popup) popup.style.display = 'none';
        let backdrop = document.querySelector('.modal-backdrop');
        if (backdrop) backdrop.remove();
    """)

    # Navigate to Duplicate DL
    page.click("a:has-text('Apply for Duplicate DL')")
    page.wait_for_load_state("networkidle")
    page.click("input[value='Continue']")
    page.wait_for_load_state("networkidle")

    # Capture captcha
    captcha_filename = f"{sid}.png"
    captcha_path = os.path.join(CAPTCHA_DIR, captcha_filename)
    page.locator("img#capimg").screenshot(path=captcha_path)

    sessions[sid] = {"p": p, "browser": browser, "context": context, "page": page}
    return sid, f"captcha/{captcha_filename}"

def finish_session(session_id, dl_number, dob, captcha_value):
    """Fill DL form, submit, and capture PDF as bytes"""
    if session_id not in sessions:
        raise Exception("Session expired")

    sess = sessions.pop(session_id)
    page, browser, context, p = sess["page"], sess["browser"], sess["context"], sess["p"]

    try:
        # DL Number
        page.wait_for_selector("input[name='dlno']", timeout=20000)
        page.fill("input[name='dlno']", dl_number)

        # DOB (force value + trigger events)
        page.wait_for_selector("input[name='dob']", timeout=20000)
        page.evaluate(
            """dob => {
                const el = document.querySelector('input[name="dob"]');
                el.value = dob;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            dob,
        )
        page.press("input[name='dob']", "Enter")

        # Captcha
        captcha_sel = None
        for sel in ["#entCaptcha", "#captcha", "input[name='captcha']",
                    "input[placeholder*='Captcha']", "input[id*='captcha']"]:
            try:
                page.wait_for_selector(sel, timeout=3000)
                captcha_sel = sel
                break
            except:
                continue
        if not captcha_sel:
            page.screenshot(path="debug_captcha_error.png")
            raise Exception("Captcha input not found (see debug_captcha_error.png)")
        page.fill(captcha_sel, captcha_value)

        # Checkbox
        page.check("input[type='checkbox']")

        # Submit
        page.click("input[value='Get DL Details']")
        try:
            page.wait_for_selector("text=Driving Licence Details", timeout=15000)
        except:
            print("[WARN] DL details page not confirmed, waiting...")
        time.sleep(3)

        # Capture PDF
        page.emulate_media(media="print")
        pdf_bytes = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        return pdf_bytes

    finally:
        try: context.close()
        except: pass
        try: browser.close()
        except: pass
        try: p.stop()
        except: pass

# ============================================================
# EXTRACTION HELPERS
# ============================================================
def format_date(date_str):
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%d/%m/%Y")
    except:
        return date_str

def safe_value(label, value):
    """Return clean value or editable placeholder"""
    return value.strip() if value and value.strip() else f'Not Found <button onclick="editField(\'{label}\')">Edit</button>'

def extract_details(pdf_bytes):
    """Extract structured DL details from PDF"""
    details = {
        "Driving Licence Number": None, "Date of Birth": None, "Name": None,
        "Father's Name": None, "Blood Group": None, "Present Address": None,
        "State": None, "RTO": None, "Class of Vehicles": None,
        "DOI": None, "VALID TILL": None, "Validity Period (Non-Transport)": None,
    }

    text_all = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i in range(min(2, len(pdf.pages))):
            page_text = pdf.pages[i].extract_text()
            if page_text:
                text_all += page_text + "\n"

    # Regex fields
    dl_match     = re.search(r"Driving Licence Number\s*\n?([A-Z0-9\s/-]+)", text_all)
    dob_match    = re.search(r"Date of Birth\s*\n?([0-9-]+)", text_all)
    name_match   = re.search(r"Name\s*:\s*(.*)", text_all)
    father_match = re.search(r"Father's Name\s*:\s*(.*)", text_all)
    blood_match  = re.search(r"Blood Group\s*:\s*([A-Z+]+)", text_all)
    rto_match    = re.search(r"RTO\s*[-:]?\s*([A-Za-z.,\s]+?)(?=\s*Class of Vehicles|\n)", text_all)

    # Present Address
    address_text = None
    addr_match = re.search(
        r"Present\s*Address\s*[:\-]?\s*([\s\S]*?)(?=\n(?:"     
        r"State\b|RTO\b|Class\s+of\s+Vehicles\b|Validity\b|"   
        r"Non\s*-\s*Transport\b|Blood\s*Group\b|Father's\s*Name\b|"
        r"Driving\s+Licence\s+Number\b|Name\b|PINCODE\b|[*]\s*State\b"
        r")|$)",
        text_all, re.IGNORECASE
    )
    if addr_match:
        address_text = addr_match.group(1).strip()

        # 🚫 Case 1: Only ":" or punctuation → missing
        if not address_text or re.fullmatch(r"[:\s,.-]*", address_text):
            address_text = None

        # 🚫 Case 2: Fake block like "here :"
        elif re.search(r"\bhere\s*[:]*$", address_text, re.IGNORECASE):
            address_text = None

        else:
            # ✅ If pincode exists, cut after pincode
            pincode_match = re.search(r"\b\d{6}\b", address_text)
            if pincode_match:
                address_text = address_text[:pincode_match.end()].strip()
            else:
                # Remove junk like http, dates, etc.
                address_text = re.split(r"http[s]?://|www\.", address_text, 1)[0].strip()
                address_text = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}.*$", "", address_text)

            # Final cleanup
            address_text = re.sub(r"\s*\n\s*", ", ", address_text)
            address_text = re.sub(r"\s{2,}", " ", address_text)
            address_text = re.sub(r"[,\s]+$", "", address_text)


    details["Present Address"] = safe_value("Present Address", address_text)

    # Class of Vehicles
    class_match = re.search(r"Class of Vehicles\s*:\s*\n?((?:.+\n?)+?)(?=Validity Period)", text_all)
    if class_match:
        raw = class_match.group(1)
        abbrs = re.findall(r"\b[A-Z]{2,6}\b", raw)
        abbrs = [x for x in abbrs if x not in {"RTO", "HASSAN", "ASST"}]
        details["Class of Vehicles"] = safe_value("Class of Vehicles", " | ".join(sorted(set(abbrs))))

    # Validity Period
    validity_match = re.search(r"Non\s*-\s*Transport\s*:\s*([0-9-]+)\s*to\s*([0-9-]+)", text_all)
    if validity_match:
        doi = format_date(validity_match.group(1).strip())
        vt  = format_date(validity_match.group(2).strip())
        details["DOI"] = safe_value("DOI", doi)
        details["VALID TILL"] = safe_value("VALID TILL", vt)
        details["Validity Period (Non-Transport)"] = f"{doi} to {vt}"

    # Assign Remaining
    details["Driving Licence Number"] = safe_value("Driving Licence Number", dl_match.group(1) if dl_match else None)
    details["Date of Birth"]          = safe_value("Date of Birth", format_date(dob_match.group(1)) if dob_match else None)
    details["Name"]                   = safe_value("Name", name_match.group(1) if name_match else None)
    details["Father's Name"]          = safe_value("Father's Name", father_match.group(1) if father_match else None)
    details["Blood Group"]            = safe_value("Blood Group", blood_match.group(1) if blood_match else None)
    details["RTO"]                    = safe_value("RTO", rto_match.group(1) if rto_match else None)
    details["State"]                  = "Karnataka"

    return details



# ===============================
# IMAGE EXTRACTION
# ===============================
def process_pdf(pdf_bytes, upload_id):
    """Extract and upload images from PDF"""
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

             # 🚫 Skip unwanted images
            if h < 40:
                continue
            if h in (50, 52):
                continue
            if (w, h) in [(251, 185), (408, 72), (221, 63)]:
                continue
            
            fn = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_p{page_num+1}_i{img_counter}.png"
            local_path = os.path.join(img_dir, fn)
            with open(local_path, "wb") as f:
                f.write(img_bytes)
            ftp_upload(local_path, f"{FTP_BASE_PATH}/images/{upload_id}/{fn}")
            # Upload to FTP
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
    # Always show loader first
    return render_template("loading.html")

@app.route("/new_session", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        sid, captcha_file = start_session()
        return render_template("new_session.html", session_id=sid, captcha_image=captcha_file)
    else:
        dlno    = request.form["dl_number"]
        dob     = request.form["dob"]
        captcha = request.form["captcha"]
        sid     = request.form["session_id"]

        pdf_bytes = finish_session(sid, dlno, dob, captcha)

        upload_id    = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        pdf_filename = f"{upload_id}_DL_Result.pdf"
        pdf_path     = os.path.join(PDF_DIR, pdf_filename)

        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        ftp_upload(pdf_path, f"{FTP_BASE_PATH}/pdfs/{pdf_filename}")

        details     = extract_details(pdf_bytes)
        page_images = process_pdf(pdf_bytes, upload_id)

        # If photo/signature missing OR DL number missing → show error
        has_photo = any(img for img in page_images if img["width"] > 0 and img["height"] > 0)
        has_sign  = len(page_images) > 1  # simple check → at least 2 images extracted

        if not has_photo or not has_sign or not details.get("Driving Licence Number") or "Not Found" in details.get("Driving Licence Number", ""):
            return render_template(
                "results.html",
                error="❌ DL details not found. Please enter correct DL Number, DOB, and Captcha.",
                upload_id=upload_id,
                page_images=[]
            )

        return render_template("results.html", details=details, upload_id=upload_id, page_images=page_images)




@app.route("/save_data", methods=["POST"])
def save_data():
    """Save extracted details + images into MySQL"""
    data = request.form.to_dict()
    upload_id = data.pop("upload_id", None)
    for k in ['Date of Birth', 'DOI', 'VALID TILL']:
        if data.get(k):
            try:
                data[k] = datetime.strptime(data[k], "%d/%m/%Y").strftime("%Y-%m-%d")
            except:
                data[k] = None
    conn = get_db_connection()
    if not conn:
        return "❌ DB failed", 500
    cursor = conn.cursor()
    try:
        # Insert DL details
        cursor.execute("""
        INSERT INTO dl_details (
            upload_id, dl_number, dob, name, father_name, blood_group,
            present_address, state, rto, class_of_vehicles,
            doi, valid_till, validity_period
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            upload_id, data.get('Driving Licence Number'), data.get('Date of Birth'),
            data.get('Name'), data.get("Father's Name"), data.get('Blood Group'),
            data.get('Present Address'), data.get('State'), data.get('RTO'),
            data.get('Class of Vehicles'), data.get('DOI'), data.get('VALID TILL'),
            data.get('Validity Period (Non-Transport)')
        ))

        # Insert image metadata
        for img_str in request.form.getlist('images_info[]'):
            img_data = img_str.split('|')
            if len(img_data) == 4:
                cursor.execute(
                    "INSERT INTO dl_images (upload_id, filename, width, height, type) VALUES (%s,%s,%s,%s,%s)",
                    (upload_id, img_data[0], int(img_data[1]), int(img_data[2]), img_data[3])
                )

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
    ensure_playwright_installed()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

