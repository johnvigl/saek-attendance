from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Cookie, Request, Response, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from pydantic import BaseModel
from datetime import date, datetime, timedelta
from typing import List, Optional
import mysql.connector
from mysql.connector import pooling, Error
import os
import csv
import io
import time
import asyncio
import smtplib
import secrets
import random
from email.mime.text import MIMEText
import bcrypt
import hashlib
import base64
from collections import defaultdict

def prehash_password(password: str) -> str:
    """SHA256 → base64 (44 bytes)"""
    sha = hashlib.sha256(password.encode('utf-8')).digest()
    return base64.b64encode(sha).decode('ascii')

def hash_password(password: str) -> str:
    pre = prehash_password(password).encode('utf-8')
    return bcrypt.hashpw(pre, bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    pre = prehash_password(plain_password).encode('utf-8')
    return bcrypt.checkpw(pre, hashed_password.encode('utf-8'))

otp_request_tracker = {}   # email -> timestamp
otp_verify_tracker = {}    # email -> timestamp
OTP_COOLDOWN = 60
VERIFY_COOLDOWN = 10

def is_rate_limited(email: str, tracker: dict, cooldown: int) -> bool:
    last = tracker.get(email)
    if last is None:
        return False
    elapsed = time.time() - last
    if elapsed < cooldown:
        return True
    # Αν πέρασε το cooldown, διαγράφουμε το entry (προαιρετικό)
    del tracker[email]
    return False

app = FastAPI(title="SAEK Attendance System API")
app.mount("/static", StaticFiles(directory="static"), name="static")

# -------------------- AUTH DEPENDENCIES --------------------
def generate_otp():
    return f"{random.randint(100000, 999999):06d}"

def generate_session_token():
    return secrets.token_urlsafe(32)

async def get_current_user(session_token: Optional[str] = Cookie(None)):
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT user_id, user_role, expires_at FROM sessions
        WHERE token = %s AND expires_at > NOW()
    """, (session_token,))
    session = cursor.fetchone()
    cursor.close()
    conn.close()
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return session

def require_role(*allowed_roles):
    def role_checker(current_user = Depends(get_current_user)):
        if current_user["user_role"] not in allowed_roles:
            raise HTTPException(403, "Access forbidden")
        return current_user
    return role_checker

# -------------------- DOMAINS FROM ENV --------------------
FRONTEND_DOMAIN = os.getenv("FRONTEND_DOMAIN", "").lower().strip()
ADMIN_DOMAIN = os.getenv("ADMIN_DOMAIN", "").lower().strip()

# Έλεγχος αν τρέχουμε σε localhost (development mode)
def is_localhost(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("localhost:")

# ---------- Helper για host check (επιστρέφει 404) ----------
def require_grammateia_host(request: Request):
    host = request.headers.get("host", "").lower()
    # Αν είναι localhost, επιτρέπεται πάντα
    if is_localhost(host):
        return
    # Αλλιώς, έλεγχος για admin domain
    if ADMIN_DOMAIN and ADMIN_DOMAIN not in host:
        raise HTTPException(status_code=404, detail="Not found")

# ---------- Middleware προστασίας docs ----------
@app.middleware("http")
async def protect_docs(request: Request, call_next):
    if request.url.path in ("/docs", "/redoc", "/openapi.json") or request.url.path.startswith(("/docs/", "/redoc/")):
        host = request.headers.get("host", "").lower()
        # Σε localhost επιτρέπεται πάντα
        if not is_localhost(host):
            # Έλεγχος admin domain (αν ορίστηκε)
            if ADMIN_DOMAIN and ADMIN_DOMAIN not in host:
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            # Έλεγχος admin session
            token = request.cookies.get("session_token")
            if not token:
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})
            try:
                conn = get_db_connection()
                cursor = conn.cursor(dictionary=True)
                cursor.execute("SELECT user_role FROM sessions WHERE token = %s AND expires_at > NOW()", (token,))
                session = cursor.fetchone()
                cursor.close()
                conn.close()
                if not session or session["user_role"] != "admin":
                    return JSONResponse(status_code=403, content={"detail": "Forbidden"})
            except Exception:
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)

# CORS - δυναμικά origins
cors_origins = []
if FRONTEND_DOMAIN and FRONTEND_DOMAIN != "localhost":
    cors_origins.append(f"https://{FRONTEND_DOMAIN}")
if ADMIN_DOMAIN and ADMIN_DOMAIN != "localhost":
    cors_origins.append(f"https://{ADMIN_DOMAIN}")
# Αν δεν υπάρχουν domains, επιτρέπουμε τα πάντα (μόνο για ανάπτυξη)
if not cors_origins:
    cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)

# ---------- Static files (HTML pages) ----------
@app.get("/teacher.html")
async def teacher_page(request: Request):
    token = request.cookies.get("session_token")
    if token:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT user_role FROM sessions WHERE token = %s AND expires_at > NOW()", (token,))
        session = cursor.fetchone()
        cursor.close()
        conn.close()
        if session and session["user_role"] == "instructor":
            return FileResponse("teacher.html")
    return FileResponse("login.html")

@app.get("/admin-settings.html")
async def admin_settings_page(request: Request, current_user = Depends(require_role("admin"))):
    require_grammateia_host(request)
    return FileResponse("admin-settings.html")

@app.get("/login.html")
async def login_page():
    return FileResponse("login.html")

@app.get("/admin-login.html")
async def admin_login_page(request: Request):
    host = request.headers.get("host", "").lower()
    # Σε localhost επιτρέπεται πάντα
    if is_localhost(host):
        return FileResponse("admin-login.html")
    # Αλλιώς, μόνο από admin domain
    if ADMIN_DOMAIN and ADMIN_DOMAIN in host:
        return FileResponse("admin-login.html")
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/admin-dashboard.html")
async def admin_dashboard_page(request: Request, current_user = Depends(require_role("admin"))):
    host = request.headers.get("host", "").lower()
    if is_localhost(host):
        return FileResponse("admin-dashboard.html")
    if ADMIN_DOMAIN and ADMIN_DOMAIN in host:
        return FileResponse("admin-dashboard.html")
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/print.html")
async def print_page(request: Request, current_user = Depends(require_role("admin"))):
    return FileResponse("print.html")

@app.get("/")
async def root(request: Request):
    host = request.headers.get("host", "").lower()
    
    # Αν είναι localhost, πάμε στο login (ή σε ό,τι θέλουμε)
    if is_localhost(host):
        # Ελέγχουμε session για να δούμε αν είναι admin ή instructor
        token = request.cookies.get("session_token")
        if token:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT user_role FROM sessions WHERE token = %s AND expires_at > NOW()", (token,))
            session = cursor.fetchone()
            cursor.close()
            conn.close()
            if session:
                if session["user_role"] == "instructor":
                    return FileResponse("teacher.html")
                elif session["user_role"] == "student":
                    return FileResponse("student.html")
                elif session["user_role"] == "admin":
                    return FileResponse("admin-dashboard.html")
        return FileResponse("login.html")
    
    # Admin dashboard για ADMIN_DOMAIN
    if ADMIN_DOMAIN and ADMIN_DOMAIN in host:
        return FileResponse("admin-dashboard.html")
    
    # Frontend για FRONTEND_DOMAIN
    if FRONTEND_DOMAIN and FRONTEND_DOMAIN in host:
        token = request.cookies.get("session_token")
        if token:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT user_role FROM sessions WHERE token = %s AND expires_at > NOW()", (token,))
            session = cursor.fetchone()
            cursor.close()
            conn.close()
            if session:
                if session["user_role"] == "instructor":
                    return FileResponse("teacher.html")
                elif session["user_role"] == "student":
                    return FileResponse("student.html")
                elif session["user_role"] == "admin":
                    return FileResponse("login.html")
        return FileResponse("login.html")
    
    # Default (π.χ. αν δεν ταιριάζει με κανένα domain)
    return {"status": "Online", "port": 5411, "database": "Connected" if connection_pool else "Disconnected"}

# -------------------- DB CONNECTION --------------------
db_config = {
    "host": os.getenv("DB_HOST", "db"),
    "database": os.getenv("DB_NAME", "saek_attendance"),
    "user": os.getenv("DB_USER", "saek_admin"),
    "password": os.getenv("DB_PASS", "saek_password"),
}

connection_pool = None

def init_connection_pool_with_retry(max_retries=15, delay=2):
    global connection_pool
    for attempt in range(max_retries):
        try:
            connection_pool = mysql.connector.pooling.MySQLConnectionPool(
                pool_name="saek_pool",
                pool_size=10,
                **db_config
            )
            print(f"✅ DB pool created (attempt {attempt+1})")
            return True
        except Error as err:
            print(f"⚠ Attempt {attempt+1}/{max_retries} failed: {err}")
            if attempt < max_retries - 1:
                time.sleep(delay)
    return False

def get_db_connection():
    global connection_pool
    if connection_pool is None:
        init_connection_pool_with_retry(max_retries=1, delay=0)
        if connection_pool is None:
            raise HTTPException(status_code=500, detail="Database not available")
    return connection_pool.get_connection()

def get_active_semester_id():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'active_semester_id'")
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row and row[0]:
        return int(row[0])
    return None

# -------------------- DATABASE INIT --------------------
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("CREATE DATABASE IF NOT EXISTS saek_attendance")
        cursor.execute("USE saek_attendance")
        
        # Πίνακας semesters (πρέπει να δημιουργηθεί πρώτα)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS semesters (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(20) NOT NULL,
                is_active BOOLEAN DEFAULT FALSE,
                start_date DATE,
                end_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(100) UNIQUE,
                password VARCHAR(255),
                role ENUM('admin', 'instructor', 'student')
            )
        """)

        # --- Δημιουργία αρχικού admin χρήστη ---
        cursor.execute("SELECT id, password FROM users WHERE role = 'admin'")
        row = cursor.fetchone()
        if not row:
            admin_plain = os.getenv("ADMIN_PASSWORD", "admin")
            admin_hash = hash_password(admin_plain)
            cursor.execute("""
                INSERT INTO users (username, password, role)
                VALUES (%s, %s, 'admin')
            """, ('admin', admin_hash))
            conn.commit()
            print("✅ Created default admin user with password admin")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teachers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                surname VARCHAR(100),
                name VARCHAR(100),
                mail VARCHAR(100),
                phone VARCHAR(20),
                semester_id INT,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (semester_id) REFERENCES semesters(id),
                UNIQUE KEY unique_mail_semester (mail, semester_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS course_units (
                id INT AUTO_INCREMENT PRIMARY KEY,
                specialty_name VARCHAR(100),
                semester VARCHAR(10),
                department VARCHAR(10),
                team VARCHAR(50),
                lesson_name VARCHAR(200),
                type_indicator CHAR(1),
                classroom VARCHAR(50),
                weekly_hours INT NOT NULL DEFAULT 1,
                semester_id INT,
                FOREIGN KEY (semester_id) REFERENCES semesters(id),
                UNIQUE KEY unique_course_semester (specialty_name, semester, department, team, lesson_name, type_indicator, semester_id, weekly_hours)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS course_assignments (
                teacher_id INT,
                course_unit_id INT,
                PRIMARY KEY (teacher_id, course_unit_id),
                FOREIGN KEY (teacher_id) REFERENCES teachers(id),
                FOREIGN KEY (course_unit_id) REFERENCES course_units(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id INT AUTO_INCREMENT PRIMARY KEY,
                course_unit_id INT,
                instructor_id INT,
                lesson_date DATE,
                syllabus_content TEXT,
                hours INT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                semester_id INT,
                FOREIGN KEY (course_unit_id) REFERENCES course_units(id),
                FOREIGN KEY (instructor_id) REFERENCES teachers(id),
                FOREIGN KEY (semester_id) REFERENCES semesters(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INT AUTO_INCREMENT PRIMARY KEY,
                amk VARCHAR(20) NOT NULL,
                surname VARCHAR(100),
                name VARCHAR(100),
                father_name VARCHAR(100),
                mother_name VARCHAR(100),
                mail VARCHAR(100),
                phone VARCHAR(20),
                specialty_name VARCHAR(100),
                semester VARCHAR(10),
                department VARCHAR(10),
                semester_id INT,
                FOREIGN KEY (semester_id) REFERENCES semesters(id),
                UNIQUE KEY unique_amk_semester (amk, semester_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS student_teams (
                student_id INT,
                team VARCHAR(50),
                PRIMARY KEY (student_id, team),
                FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS absences (
                id INT AUTO_INCREMENT PRIMARY KEY,
                lesson_id INT,
                student_id INT,
                hours_absent INT NOT NULL DEFAULT 1,
                FOREIGN KEY (lesson_id) REFERENCES lessons(id),
                FOREIGN KEY (student_id) REFERENCES students(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                email VARCHAR(100) NOT NULL,
                code VARCHAR(6) NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                INDEX idx_email_code (email, code)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                token VARCHAR(64) UNIQUE NOT NULL,
                user_id INT NOT NULL,
                user_role ENUM('instructor', 'student', 'admin') NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                INDEX idx_token (token)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                setting_key VARCHAR(100) UNIQUE NOT NULL,
                setting_value TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_senders (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(100) UNIQUE NOT NULL,
                smtp_host VARCHAR(100) NOT NULL,
                smtp_port INT NOT NULL,
                username VARCHAR(100) NOT NULL,
                password VARCHAR(255) NOT NULL,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)

        # Αν δεν υπάρχει ενεργό εξάμηνο, δημιούργησε ένα προεπιλεγμένο (π.χ. "2026α")
        cursor.execute("SELECT COUNT(*) FROM semesters")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO semesters (name, is_active) VALUES ('2026α', TRUE)")
            cursor.execute("SET @default_semester_id = LAST_INSERT_ID()")
            cursor.execute("""
                INSERT INTO settings (setting_key, setting_value) 
                VALUES ('active_semester_id', @default_semester_id)
                ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """)

        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Database tables initialized")
    except Exception as e:
        print(f"❌ Init DB error: {e}")

@app.on_event("startup")
def startup_event():
    if init_connection_pool_with_retry():
        init_db()
    else:
        print("⚠ Starting without DB – will retry on each request")

# -------------------- SMTP (placeholders) --------------------
async def send_otp_email(to_email: str, otp_code: str) -> bool:
    """
    Στέλνει το OTP email στο background χρησιμοποιώντας thread pool.
    """
    # Βρες τον πρώτο ενεργό λογαριασμό αποστολής (προτεραιότητα σε 'no-reply' αν υπάρχει)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT * FROM email_senders 
        WHERE is_active = TRUE 
        ORDER BY CASE WHEN name = 'no-reply' THEN 1 ELSE 2 END, id
        LIMIT 1
    """)
    sender = cursor.fetchone()

    # 2. Βρες το όνομα μονάδας από settings
    cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'unit_name'")
    row = cursor.fetchone()
    unit_name = row["setting_value"] if row and row["setting_value"] else None

    cursor.close()
    conn.close()

    if not sender:
        print("❌ No active email sender found. OTP not sent.")
        return False

    # Προετοιμασία του subject
    if unit_name:
        subject = f"Κωδικός σύνδεσης - {unit_name}"
    else:
        subject = "Κωδικός σύνδεσης - ΣΑΕΚ"

    def _send():
        smtp_host = sender["smtp_host"]
        smtp_port = sender["smtp_port"]
        smtp_user = sender["username"]
        smtp_pass = sender["password"]

        msg = MIMEText(f"Ο κωδικός σας για την εφαρμογή απουσιών είναι: {otp_code}\nΙσχύει για 5 λεπτά.")
        msg['Subject'] = subject
        msg['From'] = smtp_user
        msg['To'] = to_email

        try:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True
        except Exception as e:
            print(f"Email error: {e}")
            return False

    # Εκτέλεση της blocking λειτουργίας σε ξεχωριστό thread
    return await asyncio.to_thread(_send)

def get_cookie_settings(request: Request):
    """Επιστρέφει domain και secure flag για το cookie ανάλογα με το host."""
    host = request.headers.get("host", "").lower()
    # Αν είναι localhost ή IP, απενεργοποιούμε domain και secure
    if "localhost" in host or "127.0.0.1" in host or host.startswith("192.168.") or host.startswith("10."):
        return {"domain": None, "secure": False}
    else:
        # Για παραγωγή, χρησιμοποιούμε το base domain (π.χ. saek_example.gr)
        # Αφαιρούμε το subdomain (π.χ. grammateia. ή apousies.)
        parts = host.split('.')
        if len(parts) >= 2:
            base_domain = '.'.join(parts[-2:])  # παίρνουμε τα δύο τελευταία τμήματα
            return {"domain": f".{base_domain}", "secure": True}
        else:
            return {"domain": None, "secure": False}

# -------------------- AUTH ENDPOINTS --------------------
@app.post("/auth/request-otp")
async def request_otp(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    email = data.get("email")
    if not email:
        raise HTTPException(400, "Email required")

    # Έλεγχος rate limiting ανά email
    if is_rate_limited(email, otp_request_tracker, OTP_COOLDOWN):
        last = otp_request_tracker[email]
        wait = int(OTP_COOLDOWN - (time.time() - last)) + 1
        raise HTTPException(429, f"Περιμένετε {wait} δευτερόλεπτα για νέο κωδικό.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM teachers WHERE mail = %s", (email,))
    teacher = cursor.fetchone()
    cursor.execute("SELECT id FROM students WHERE mail = %s", (email,))
    student = cursor.fetchone()
    email_exists = teacher or student

    if email_exists:
        otp = generate_otp()
        expires = datetime.utcnow() + timedelta(minutes=5)
        cursor.execute("""
            INSERT INTO otp_codes (email, code, expires_at, used)
            VALUES (%s, %s, %s, FALSE)
        """, (email, otp, expires))
        conn.commit()
        # Προγραμματίζουμε την αποστολή email στο background
        background_tasks.add_task(send_otp_email, email, otp)
        # Καταγραφή timestamp για rate limiting
        otp_request_tracker[email] = time.time()
    else:
        # Δεν υπάρχει email – δεν κάνουμε τίποτα, αλλά ούτε και αποστολή
        # Για rate limiting, καταγράφουμε το timestamp (αποτρέπει brute force)
        otp_request_tracker[email] = time.time()
        import logging
        logging.info(f"Request for unknown email: {email}")
    cursor.close()
    conn.close()
    # ΕΠΙΣΤΡΕΦΟΥΜΕ μετά από λίγο (σταθερό χρόνο) χωρίς να περιμένουμε το email
    await asyncio.sleep(4)
    return {"message": "Εφόσον το email σας είναι καταχωρημένο, έχετε λάβει κωδικό OTP (ελέγξτε το inbox σας)"}

@app.post("/auth/verify-otp")
async def verify_otp(request: Request, response: Response):
    data = await request.json()
    email = data.get("email")
    code = data.get("code")
    if not email or not code:
        raise HTTPException(400, "Email and code required")

    # Rate limiting ανά email για verify
    if is_rate_limited(email, otp_verify_tracker, VERIFY_COOLDOWN):
        last = otp_verify_tracker[email]
        wait = int(VERIFY_COOLDOWN - (time.time() - last)) + 1
        raise HTTPException(429, f"Περιμένετε {wait} δευτερόλεπτα για επαλήθευση.")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT * FROM otp_codes
        WHERE email = %s AND code = %s AND used = FALSE AND expires_at > NOW()
        ORDER BY id DESC LIMIT 1
    """, (email, code))
    otp_record = cursor.fetchone()
    if not otp_record:
        # Καταγράφουμε την αποτυχία για rate limiting
        otp_verify_tracker[email] = time.time()
        cursor.close()
        conn.close()
        raise HTTPException(401, "Λανθασμένος κωδικός ή το email δεν είναι εγγεγραμμένο")
    cursor.execute("UPDATE otp_codes SET used = TRUE WHERE id = %s", (otp_record["id"],))
    cursor.execute("SELECT id FROM teachers WHERE mail = %s", (email,))
    teacher = cursor.fetchone()
    cursor.execute("SELECT id FROM students WHERE mail = %s", (email,))
    student = cursor.fetchone()
    cursor.execute("SELECT id FROM users WHERE username = %s", (email,))
    user = cursor.fetchone()
    if not user:
        if teacher:
            role = "instructor"
        elif student:
            role = "student"
        else:
            raise HTTPException(404, "Λανθασμένος κωδικός ή το email δεν είναι εγγεγραμμένο")
        cursor.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", (email, 'changeme', role))
        user_id = cursor.lastrowid
    else:
        user_id = user["id"]
    if teacher:
        cursor.execute("UPDATE teachers SET user_id = %s WHERE id = %s", (user_id, teacher["id"]))
        user_role = "instructor"
    elif student:
        user_role = "student"
    else:
        raise HTTPException(404, "User not found")
    token = generate_session_token()
    expires = datetime.utcnow() + timedelta(days=7)
    cursor.execute("""
        INSERT INTO sessions (token, user_id, user_role, expires_at)
        VALUES (%s, %s, %s, %s)
    """, (token, user_id, user_role, expires))
    conn.commit()
    cursor.close()
    conn.close()
    otp_verify_tracker[email] = time.time()
    resp = JSONResponse(content={"message": "Login successful", "role": user_role})
    cookie_settings = get_cookie_settings(request)
    rest.set_cookie(key="session_token", value=token, httponly=True, secure=cookie_settings["secure"], samesite="lax", domain=cookie_settings["domain"],max_age=7*24*3600)
    return resp

@app.post("/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE token = %s", (token,))
        conn.commit()
        cursor.close()
        conn.close()
    response.delete_cookie("session_token")
    return {"message": "Logged out"}

@app.get("/auth/check")
async def check_auth(current_user = Depends(get_current_user)):
    return {"authenticated": True, "role": current_user["user_role"], "user_id": current_user["user_id"]}

# -------------------- MODELS --------------------
class AbsenceEntry(BaseModel):
    student_id: int
    hours_absent: int

class AttendanceEntry(BaseModel):
    course_unit_id: int
    instructor_id: int
    total_hours: int
    syllabus: str
    absences: List[AbsenceEntry]

# -------------------- IMPORT ENDPOINTS --------------------
@app.post("/admin/import-teachers")
async def import_teachers(file: UploadFile = File(...), current_user = Depends(require_role("admin"))):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        for row in reader:
            email = row.get('mail') or row.get('email')
            if not email:
                continue
            
            cursor.execute("SELECT id FROM users WHERE username = %s", (email,))
            user = cursor.fetchone()
            if not user:
                cursor.execute(
                    "INSERT INTO users (username, password, role) VALUES (%s, %s, 'instructor')",
                    (email, 'changeme')
                )
                user_id = cursor.lastrowid
            else:
                user_id = user[0]
            
            cursor.execute("""
                INSERT INTO teachers (user_id, surname, name, mail, phone, semester_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                user_id = VALUES(user_id),
                surname = VALUES(surname),
                name = VALUES(name),
                phone = VALUES(phone)
                -- Δεν ενημερώνουμε το semester_id (παραμένει το αρχικό)
            """, (user_id, row.get('surname'), row.get('name'), email, row.get('phone', ''), active_semester_id))
        
        conn.commit()
        return {"message": "Teachers imported with correct user linking and semester."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/teachers")
async def admin_add_teacher(data: dict, current_user = Depends(require_role("admin"))):
    surname = data.get("surname")
    name = data.get("name")
    mail = data.get("mail")
    phone = data.get("phone", "")
    if not surname or not name or not mail:
        raise HTTPException(400, "Missing required fields: surname, name, mail")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. Πάρε το ενεργό εξάμηνο
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        # 2. Get or create user
        cursor.execute("SELECT id FROM users WHERE username = %s", (mail,))
        user = cursor.fetchone()
        if not user:
            cursor.execute(
                "INSERT INTO users (username, password, role) VALUES (%s, %s, 'instructor')",
                (mail, 'changeme')
            )
            user_id = cursor.lastrowid
        else:
            user_id = user[0]
        
        # 3. Insert teacher with semester_id
        cursor.execute("""
            INSERT INTO teachers (user_id, surname, name, mail, phone, semester_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, surname, name, mail, phone, active_semester_id))
        conn.commit()
        return {"message": "Teacher added successfully"}
    except mysql.connector.IntegrityError as e:
        conn.rollback()
        if "Duplicate entry" in str(e):
            raise HTTPException(400, "Teacher with this email already exists in this semester")
        raise HTTPException(400, str(e))
    finally:
        cursor.close()
        conn.close()

# -------------------- IMPORT CLASSES --------------------
@app.post("/admin/import-classes")
async def import_classes(file: UploadFile = File(...), current_user = Depends(require_role("admin"))):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")

        for row in reader:
            # Ανάγνωση weekly_hours (προαιρετικό, default 1)
            weekly_hours_str = row.get('weekly_hours', '1').strip()
            try:
                weekly_hours = int(weekly_hours_str)
                if weekly_hours < 1:
                    weekly_hours = 1
            except:
                weekly_hours = 1

            # Αναζήτηση αν υπάρχει το course_unit (ίδιο εξάμηνο και ίδιες weekly_hours)
            cursor.execute("""
                SELECT id FROM course_units 
                WHERE specialty_name=%s AND semester=%s AND department=%s 
                AND team=%s AND lesson_name=%s AND type_indicator=%s
                AND semester_id = %s AND weekly_hours = %s
            """, (
                row['specialty_name'], row['semester'], row['department'],
                row.get('team', ''), row['lesson_name'], row.get('type_indicator', ''),
                active_semester_id, weekly_hours
            ))
            unit = cursor.fetchone()
            cursor.fetchall()

            if not unit:
                cursor.execute("""
                    INSERT INTO course_units 
                    (specialty_name, semester, department, team, lesson_name, type_indicator, classroom, semester_id, weekly_hours)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    row['specialty_name'], row['semester'], row['department'],
                    row.get('team', ''), row['lesson_name'], row.get('type_indicator', ''), row.get('classroom', ''),
                    active_semester_id, weekly_hours
                ))
                unit_id = cursor.lastrowid
            else:
                unit_id = unit[0]

            # Σύνδεση με καθηγητή (ίδιο εξάμηνο)
            teacher_surname = row.get('surname')
            teacher_name = row.get('name')
            if teacher_surname and teacher_name:
                cursor.execute(
                    "SELECT id FROM teachers WHERE surname=%s AND name=%s AND semester_id = %s",
                    (teacher_surname, teacher_name, active_semester_id)
                )
                teacher_row = cursor.fetchone()
                cursor.fetchall()
                if teacher_row:
                    teacher_id = teacher_row[0]
                    cursor.execute(
                        "INSERT IGNORE INTO course_assignments (teacher_id, course_unit_id) VALUES (%s, %s)",
                        (teacher_id, unit_id)
                    )
        conn.commit()
        return {"message": "Επιτυχής εισαγωγή μαθημάτων και συνδιδασκαλιών."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Σφάλμα εισαγωγής: {str(e)}")
    finally:
        cursor.close()
        conn.close()

# -------------------- IMPORT STUDENTS --------------------
@app.post("/admin/import-students")
async def import_students(file: UploadFile = File(...), current_user = Depends(require_role("admin"))):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        for row in reader:
            amk = row.get('amk', '').strip()
            if not amk:
                continue
            cursor.execute("""
                INSERT INTO students
                (amk, surname, name, father_name, mother_name, mail, phone, specialty_name, semester, department, semester_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                surname = VALUES(surname),
                name = VALUES(name),
                father_name = VALUES(father_name),
                mother_name = VALUES(mother_name),
                mail = VALUES(mail),
                phone = VALUES(phone),
                specialty_name = VALUES(specialty_name),
                semester = VALUES(semester),
                department = VALUES(department)
                -- semester_id δεν ενημερώνεται (παραμένει το αρχικό)
            """, (
                amk,
                row.get('surname', ''),
                row.get('name', ''),
                row.get('father_name', ''),
                row.get('mother_name', ''),
                row.get('mail', ''),
                row.get('phone', ''),
                row.get('specialty_name', ''),
                row.get('semester', ''),
                row.get('department', ''),
                active_semester_id
            ))
            cursor.execute("SELECT id FROM students WHERE amk=%s", (amk,))
            student_id = cursor.fetchone()[0]
            cursor.execute("DELETE FROM student_teams WHERE student_id=%s", (student_id,))
            teams_str = row.get('team', '').strip()
            if teams_str:
                for team in teams_str.split(','):
                    team = team.strip()
                    if team:
                        cursor.execute(
                            "INSERT INTO student_teams (student_id, team) VALUES (%s, %s)",
                            (student_id, team)
                        )
        conn.commit()
        return {"message": "Επιτυχής εισαγωγή μαθητών και ομάδων."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Σφάλμα εισαγωγής: {str(e)}")
    finally:
        cursor.close()
        conn.close()

# -------------------- SUBMIT ATTENDANCE --------------------
@app.post("/attendance/submit")
async def submit_attendance(entry: AttendanceEntry, current_user = Depends(require_role("instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        # 1. Find teacher.id from user_id
        cursor.execute("SELECT id FROM teachers WHERE user_id = %s AND semester_id = %s", 
                       (current_user["user_id"], active_semester_id))
        teacher = cursor.fetchone()
        if not teacher:
            raise HTTPException(403, "Δεν βρέθηκε εκπαιδευτής για τον λογαριασμό σας στο τρέχον εξάμηνο")
        instructor_id = teacher["id"]

        # 2. Verify teacher teaches this course (and course belongs to active semester)
        cursor.execute("""
            SELECT 1 FROM course_assignments ca
            JOIN course_units cu ON ca.course_unit_id = cu.id
            WHERE ca.teacher_id = %s AND ca.course_unit_id = %s AND cu.semester_id = %s
        """, (instructor_id, entry.course_unit_id, active_semester_id))
        if not cursor.fetchone():
            raise HTTPException(403, "Δεν διδάσκετε αυτό το μάθημα στο τρέχον εξάμηνο")

        today = date.today()
        # 3. Check if a lesson already exists for this course today (only in active semester)
        cursor.execute("""
            SELECT l.id FROM lessons l
            JOIN course_units cu ON l.course_unit_id = cu.id
            WHERE l.course_unit_id = %s AND l.lesson_date = %s AND cu.semester_id = %s
        """, (entry.course_unit_id, today, active_semester_id))
        existing_lesson = cursor.fetchone()

        if existing_lesson:
            lesson_id = existing_lesson["id"]
            cursor.execute("""
                UPDATE lessons
                SET hours = %s, syllabus_content = %s
                WHERE id = %s
            """, (entry.total_hours, entry.syllabus, lesson_id))
            cursor.execute("DELETE FROM absences WHERE lesson_id = %s", (lesson_id,))
        else:
            cursor.execute("""
                INSERT INTO lessons (course_unit_id, instructor_id, lesson_date, syllabus_content, hours, semester_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (entry.course_unit_id, instructor_id, today, entry.syllabus, entry.total_hours, active_semester_id))
            lesson_id = cursor.lastrowid

        for abs_item in entry.absences:
            cursor.execute("""
                INSERT INTO absences (lesson_id, student_id, hours_absent)
                VALUES (%s, %s, %s)
            """, (lesson_id, abs_item.student_id, abs_item.hours_absent))

        conn.commit()
        return {
            "status": "Success",
            "lesson_id": lesson_id,
            "date": today.isoformat(),
            "total_hours": entry.total_hours,
            "absences_registered": len(entry.absences),
            "updated": existing_lesson is not None
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        print(f"Submit attendance error: {e}")
        raise HTTPException(500, str(e))
    finally:
        cursor.close()
        conn.close()

# -------------------- DELETE ENDPOINTS --------------------
@app.delete("/admin/teachers/{teacher_id}")
async def admin_delete_teacher(teacher_id: int, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM course_assignments WHERE teacher_id = %s", (teacher_id,))
        cursor.execute("DELETE FROM teachers WHERE id = %s", (teacher_id,))
        conn.commit()
        return {"message": "Teacher deleted"}
    finally:
        cursor.close()
        conn.close()

@app.delete("/admin/students/{student_id}")
async def admin_delete_student(student_id: int, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM student_teams WHERE student_id = %s", (student_id,))
        cursor.execute("DELETE FROM absences WHERE student_id = %s", (student_id,))
        cursor.execute("DELETE FROM students WHERE id = %s", (student_id,))
        conn.commit()
        return {"message": "Student deleted"}
    finally:
        cursor.close()
        conn.close()

@app.delete("/admin/course-units/{unit_id}")
async def admin_delete_course_unit(unit_id: int, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM course_assignments WHERE course_unit_id = %s", (unit_id,))
        count = cursor.fetchone()[0]
        if count > 0:
            raise HTTPException(400, "Δεν μπορεί να διαγραφεί γιατί υπάρχουν ενεργές αναθέσεις")
        cursor.execute("SELECT COUNT(*) FROM lessons WHERE course_unit_id = %s", (unit_id,))
        count = cursor.fetchone()[0]
        if count > 0:
            raise HTTPException(400, "Δεν μπορεί να διαγραφεί γιατί υπάρχουν καταγεγραμμένες διδασκαλίες/απουσίες")
        cursor.execute("DELETE FROM course_units WHERE id = %s", (unit_id,))
        conn.commit()
        return {"message": "Το μάθημα διαγράφηκε επιτυχώς"}
    finally:
        cursor.close()
        conn.close()

@app.delete("/attendance/today/{course_unit_id}")
async def delete_today_lesson(course_unit_id: int, current_user = Depends(require_role("instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        cursor.execute("SELECT id FROM teachers WHERE user_id = %s AND semester_id = %s", 
                       (current_user["user_id"], active_semester_id))
        teacher = cursor.fetchone()
        if not teacher:
            raise HTTPException(403, "Δεν βρέθηκε εκπαιδευτής στο τρέχον εξάμηνο")
        instructor_id = teacher["id"]
        
        cursor.execute("""
            SELECT 1 FROM course_assignments ca
            JOIN course_units cu ON ca.course_unit_id = cu.id
            WHERE ca.teacher_id = %s AND ca.course_unit_id = %s AND cu.semester_id = %s
        """, (instructor_id, course_unit_id, active_semester_id))
        if not cursor.fetchone():
            raise HTTPException(403, "Δεν διδάσκετε αυτό το μάθημα στο τρέχον εξάμηνο")
        
        today = date.today()
        cursor.execute("""
            SELECT l.id FROM lessons l
            JOIN course_units cu ON l.course_unit_id = cu.id
            WHERE l.course_unit_id = %s AND l.lesson_date = %s AND cu.semester_id = %s
        """, (course_unit_id, today, active_semester_id))
        lesson = cursor.fetchone()
        if not lesson:
            raise HTTPException(404, "Δεν υπάρχει καταχώρηση για σήμερα")
        
        cursor.execute("DELETE FROM absences WHERE lesson_id = %s", (lesson["id"],))
        cursor.execute("DELETE FROM lessons WHERE id = %s", (lesson["id"],))
        conn.commit()
        return {"message": "Η καταχώρηση της σημερινής διδασκαλίας διαγράφηκε"}
    finally:
        cursor.close()
        conn.close()

# -------------------- VIEW ENDPOINTS (προστασία) --------------------
@app.get("/teachers")
async def list_teachers(current_user = Depends(require_role("admin", "instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            return {"teachers": []}
        
        if current_user["user_role"] == "admin":
            cursor.execute("""
                SELECT id, surname, name, mail, phone, user_id 
                FROM teachers 
                WHERE semester_id = %s
                ORDER BY surname, name
            """, (semester_id,))
        else:  # instructor
            cursor.execute("""
                SELECT id, surname, name, mail, phone, user_id 
                FROM teachers 
                WHERE user_id = %s AND semester_id = %s
            """, (current_user["user_id"], semester_id))
        teachers = cursor.fetchall()
        return {"teachers": teachers}
    finally:
        cursor.close()
        conn.close()

# -------------------- ADMIN CRUD ENDPOINTS --------------------
@app.get("/admin/teachers")
async def admin_list_teachers(
    specialty: Optional[str] = None,
    department: Optional[str] = None,
    semester_id: Optional[int] = None,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Αν δεν δόθηκε semester_id, χρησιμοποίησε το ενεργό
        if semester_id is None:
            semester_id = get_active_semester_id()
            if semester_id is None:
                raise HTTPException(400, "No active semester configured")
        
        query = "SELECT id, surname, name, mail, phone, user_id FROM teachers WHERE semester_id = %s"
        params = [semester_id]
        
        if specialty:
            query += " AND specialty_name = %s"
            params.append(specialty)
        if department:
            query += " AND department = %s"
            params.append(department)
        query += " ORDER BY surname, name"
        
        cursor.execute(query, params)
        teachers = cursor.fetchall()
        return {"teachers": teachers}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/teachers/{teacher_id}")
async def admin_update_teacher(teacher_id: int, data: dict, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 1. Αν αλλάζει το email, πρέπει να ενημερωθεί και το users.username
        new_mail = data.get("mail")
        if new_mail:
            # Βρες το υπάρχον user_id
            cursor.execute("SELECT user_id FROM teachers WHERE id = %s", (teacher_id,))
            row = cursor.fetchone()
            if row and row["user_id"]:
                # Ενημέρωσε τον πίνακα users
                cursor.execute("UPDATE users SET username = %s WHERE id = %s", (new_mail, row["user_id"]))
        
        # 2. Ενημέρωση των υπολοίπων πεδίων στον teachers
        allowed_fields = ["surname", "name", "mail", "phone"]
        updates = []
        values = []
        for field in allowed_fields:
            if field in data:
                updates.append(f"{field} = %s")
                values.append(data[field])
        if not updates:
            raise HTTPException(400, "No fields to update")
        values.append(teacher_id)
        cursor.execute(f"UPDATE teachers SET {', '.join(updates)} WHERE id = %s", values)
        conn.commit()
        return {"message": "Teacher updated"}
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/students")
async def admin_list_students(
    specialty: Optional[str] = None,
    semester: Optional[str] = None,   # το παλιό string (π.χ. "Α")
    department: Optional[str] = None,
    team: Optional[str] = None,
    semester_id: Optional[int] = None,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Αν δεν δόθηκε semester_id, χρησιμοποίησε το ενεργό
        if semester_id is None:
            semester_id = get_active_semester_id()
            if semester_id is None:
                raise HTTPException(400, "No active semester configured")
        
        query = """
            SELECT s.id, s.amk, s.surname, s.name, s.father_name, s.mother_name,
                   s.mail, s.phone, s.specialty_name, s.semester, s.department,
                   GROUP_CONCAT(st.team) AS teams
            FROM students s
            LEFT JOIN student_teams st ON s.id = st.student_id
            WHERE s.semester_id = %s
        """
        params = [semester_id]
        
        if specialty:
            query += " AND s.specialty_name = %s"
            params.append(specialty)
        if semester:
            query += " AND s.semester = %s"
            params.append(semester)
        if department:
            query += " AND s.department = %s"
            params.append(department)
        if team:
            query += " AND st.team = %s"
            params.append(team)
        query += " GROUP BY s.id ORDER BY s.surname, s.name"
        
        cursor.execute(query, params)
        students = cursor.fetchall()
        for s in students:
            s["teams"] = s["teams"].split(",") if s["teams"] else []
        return {"students": students}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/students/{student_id}")
async def admin_update_student(student_id: int, data: dict, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        allowed_fields = ["amk", "surname", "name", "father_name", "mother_name", "mail", "phone", "specialty_name", "semester", "department"]
        updates = []
        values = []
        for field in allowed_fields:
            if field in data:
                updates.append(f"{field} = %s")
                values.append(data[field])
        if updates:
            values.append(student_id)
            cursor.execute(f"UPDATE students SET {', '.join(updates)} WHERE id = %s", values)
        if "teams" in data:
            cursor.execute("DELETE FROM student_teams WHERE student_id = %s", (student_id,))
            for team in data["teams"]:
                if team and team.strip():
                    cursor.execute("INSERT INTO student_teams (student_id, team) VALUES (%s, %s)", (student_id, team.strip()))
        conn.commit()
        return {"message": "Student updated"}
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/course-units")
async def admin_list_course_units(
    specialty: Optional[str] = None,
    semester_str: Optional[str] = None,   # το υπάρχον πεδίο (π.χ. "Α", "Β")
    department: Optional[str] = None,
    team: Optional[str] = None,
    semester_id: Optional[int] = None,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Αν δεν δόθηκε semester_id, χρησιμοποιούμε το ενεργό
        if semester_id is None:
            semester_id = get_active_semester_id()
            if semester_id is None:
                raise HTTPException(400, "No active semester configured")
        
        query = "SELECT * FROM course_units WHERE semester_id = %s"
        params = [semester_id]
        
        if specialty:
            query += " AND specialty_name = %s"
            params.append(specialty)
        if semester_str:
            query += " AND semester = %s"
            params.append(semester_str)
        if department:
            query += " AND department = %s"
            params.append(department)
        if team:
            query += " AND team = %s"
            params.append(team)
        query += " ORDER BY specialty_name, semester, department, team, lesson_name"
        
        cursor.execute(query, params)
        units = cursor.fetchall()
        return {"course_units": units}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/course-units/{unit_id}")
async def admin_update_course_unit(unit_id: int, data: dict, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        allowed_fields = ["specialty_name", "semester", "department", "team", "lesson_name", "type_indicator", "classroom", "weekly_hours"]
        updates = []
        values = []
        for field in allowed_fields:
            if field in data:
                # Ειδικός έλεγχος για weekly_hours
                if field == "weekly_hours":
                    val = data[field]
                    try:
                        val = int(val)
                        if val < 1:
                            val = 1
                    except:
                        val = 1
                    updates.append(f"{field} = %s")
                    values.append(val)
                else:
                    updates.append(f"{field} = %s")
                    values.append(data[field])
        if not updates:
            raise HTTPException(400, "No fields to update")
        values.append(unit_id)
        cursor.execute(f"UPDATE course_units SET {', '.join(updates)} WHERE id = %s", values)
        conn.commit()
        return {"message": "Course unit updated"}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/course-units")
async def admin_add_course_unit(data: dict, current_user = Depends(require_role("admin"))):
    lesson_name = data.get("lesson_name")
    specialty_name = data.get("specialty_name")
    semester_str = data.get("semester")
    department = data.get("department")
    team = data.get("team", "")
    type_indicator = data.get("type_indicator", "")
    classroom = data.get("classroom", "")
    weekly_hours = data.get("weekly_hours", 1)
    try:
        weekly_hours = int(weekly_hours)
        if weekly_hours < 1:
            weekly_hours = 1
    except:
        weekly_hours = 1

    if not lesson_name or not specialty_name or not semester_str or not department:
        raise HTTPException(400, "Missing required fields: lesson_name, specialty_name, semester, department")

    semester_id = get_active_semester_id()
    if semester_id is None:
        raise HTTPException(400, "No active semester configured")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO course_units (lesson_name, specialty_name, semester, department, team, type_indicator, classroom, semester_id, weekly_hours)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (lesson_name, specialty_name, semester_str, department, team, type_indicator, classroom, semester_id, weekly_hours))
        conn.commit()
        return {"message": "Course unit added successfully", "id": cursor.lastrowid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        cursor.close()
        conn.close()

# Στο main.py, τροποποίησε το admin_list_assignments ώστε να επιστρέφει cu.weekly_hours
@app.get("/admin/assignments")
async def admin_list_assignments(
    teacher_id: Optional[int] = None,
    course_unit_id: Optional[int] = None,
    specialty: Optional[str] = None,
    department: Optional[str] = None,
    semester_id: Optional[int] = None,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if semester_id is None:
            semester_id = get_active_semester_id()
            if semester_id is None:
                raise HTTPException(400, "No active semester configured")
        
        query = """
            SELECT 
                ca.teacher_id,
                ca.course_unit_id,
                CONCAT(cu.lesson_name, ' (', cu.type_indicator, ')') AS lesson_with_type,
                CONCAT(t.surname, ' ', t.name) AS teacher_name,
                cu.specialty_name,
                cu.department,
                cu.team,
                cu.weekly_hours
            FROM course_assignments ca
            JOIN teachers t ON ca.teacher_id = t.id
            JOIN course_units cu ON ca.course_unit_id = cu.id
            WHERE cu.semester_id = %s
        """
        params = [semester_id]
        if teacher_id and teacher_id > 0:
            query += " AND ca.teacher_id = %s"
            params.append(teacher_id)
        if course_unit_id and course_unit_id > 0:
            query += " AND ca.course_unit_id = %s"
            params.append(course_unit_id)
        if specialty and specialty.strip():
            query += " AND cu.specialty_name = %s"
            params.append(specialty)
        if department and department.strip():
            query += " AND cu.department = %s"
            params.append(department)
        query += " ORDER BY cu.specialty_name, cu.semester, cu.department, cu.lesson_name, t.surname, t.name"
        cursor.execute(query, params)
        assignments = cursor.fetchall()
        return {"assignments": assignments}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/assignments")
async def admin_add_assignment(data: dict, current_user = Depends(require_role("admin"))):
    teacher_id = data.get("teacher_id")
    course_unit_id = data.get("course_unit_id")
    if not teacher_id or not course_unit_id:
        raise HTTPException(400, "teacher_id and course_unit_id required")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT IGNORE INTO course_assignments (teacher_id, course_unit_id) VALUES (%s, %s)", (teacher_id, course_unit_id))
        conn.commit()
        return {"message": "Assignment added"}
    finally:
        cursor.close()
        conn.close()

@app.delete("/admin/assignments")
async def admin_delete_assignment(data: dict, current_user = Depends(require_role("admin"))):
    teacher_id = data.get("teacher_id")
    course_unit_id = data.get("course_unit_id")
    if not teacher_id or not course_unit_id:
        raise HTTPException(400, "teacher_id and course_unit_id required")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM course_assignments WHERE teacher_id = %s AND course_unit_id = %s", (teacher_id, course_unit_id))
        conn.commit()
        return {"message": "Assignment deleted"}
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/absences-overview")
async def admin_absences_overview(
    specialty_name: Optional[str] = None,
    semester: Optional[str] = None,
    semester_id: Optional[int] = None,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if semester_id is None:
            semester_id = get_active_semester_id()
            if semester_id is None:
                raise HTTPException(400, "No active semester configured")

        # 1. Λίστα όλων των συνδυασμών ειδικότητας/εξαμήνου
        cursor.execute("""
            SELECT DISTINCT specialty_name, semester
            FROM students
            WHERE semester_id = %s
            ORDER BY specialty_name, semester
        """, (semester_id,))
        rows = cursor.fetchall()
        specialty_semesters = [
            {"value": f"{row['specialty_name']}|{row['semester']}", "display": f"{row['specialty_name']} {row['semester']}"}
            for row in rows
        ]

        if not specialty_name or not semester:
            return {"specialty_semesters": specialty_semesters, "courses": [], "students": []}

        # 2. Μοναδικά μαθήματα (lesson_name)
        cursor.execute("""
            SELECT DISTINCT lesson_name
            FROM course_units
            WHERE specialty_name = %s AND semester = %s AND semester_id = %s
            ORDER BY lesson_name
        """, (specialty_name, semester, semester_id))
        courses = [row["lesson_name"] for row in cursor.fetchall()]

        # 3. Για κάθε μάθημα, υπολόγισε τις ώρες ανά τμήμα και ομάδα (ξεχωριστά)
        #    Δομή: course_hours_by_dept_team[lesson_name][department][team] = total_weekly_hours
        course_hours_by_dept_team = {}
        for lesson_name in courses:
            cursor.execute("""
                SELECT department, team, SUM(weekly_hours) AS total_weekly
                FROM course_units
                WHERE specialty_name = %s AND semester = %s AND lesson_name = %s AND semester_id = %s
                GROUP BY department, team
            """, (specialty_name, semester, lesson_name, semester_id))
            rows_dept_team = cursor.fetchall()
            dept_team_hours = {}
            for r in rows_dept_team:
                dept = r['department']
                team = r['team'] or ''  # κενό string για τις εγγραφές χωρίς ομάδα
                total_weekly = r['total_weekly'] or 0
                if dept not in dept_team_hours:
                    dept_team_hours[dept] = {}
                dept_team_hours[dept][team] = total_weekly * 15
            course_hours_by_dept_team[lesson_name] = dept_team_hours

        # 4. Μαθητές της ειδικότητας/εξαμήνου
        cursor.execute("""
            SELECT s.id, s.amk, s.surname, s.name, s.department, s.mail,
                   GROUP_CONCAT(st.team) AS teams
            FROM students s
            LEFT JOIN student_teams st ON s.id = st.student_id
            WHERE s.specialty_name = %s AND s.semester = %s AND s.semester_id = %s
            GROUP BY s.id
            ORDER BY s.surname, s.name
        """, (specialty_name, semester, semester_id))
        students = cursor.fetchall()

        result_students = []
        for student in students:
            student_id = student["id"]
            student_dept = student["department"]
            # Λίστα ομάδων του μαθητή (μπορεί να έχει μία ή περισσότερες, αλλά συνήθως μία)
            teams_list = student["teams"].split(",") if student["teams"] else []
            absences_by_course = {}

            for lesson_name in courses:
                # 4α. Βρες τις ώρες για το τμήμα του μαθητή
                dept_hours_map = course_hours_by_dept_team.get(lesson_name, {}).get(student_dept, {})
                
                # 4β. Υπολόγισε τις συνολικές ώρες για τον μαθητή σε αυτό το μάθημα
                # Ξεκινάμε με τις ώρες χωρίς ομάδα (κλειδί '')
                total_hours_for_student = dept_hours_map.get('', 0)
                
                # Προσθέτουμε τις ώρες για κάθε ομάδα του μαθητή
                for team in teams_list:
                    team_key = team if team is not None else ''
                    total_hours_for_student += dept_hours_map.get(team_key, 0)

                # 4γ. Υπολόγισε τις απουσίες του μαθητή σε αυτό το μάθημα (μόνο για το τμήμα του)
                cursor.execute("""
                    SELECT COALESCE(SUM(a.hours_absent), 0) AS total_absences
                    FROM absences a
                    JOIN lessons l ON a.lesson_id = l.id
                    JOIN course_units cu ON l.course_unit_id = cu.id
                    WHERE a.student_id = %s
                      AND cu.specialty_name = %s
                      AND cu.semester = %s
                      AND cu.lesson_name = %s
                      AND cu.department = %s
                      AND cu.semester_id = %s
                """, (student_id, specialty_name, semester, lesson_name, student_dept, semester_id))
                abs_row = cursor.fetchone()
                absences = abs_row["total_absences"] or 0
                fraction = absences / total_hours_for_student if total_hours_for_student > 0 else 0

                absences_by_course[lesson_name] = {
                    "absent": absences,
                    "total": total_hours_for_student,
                    "fraction": fraction
                }

            student["specialty_dept"] = f"{specialty_name} - {student['department']}"
            student["absences_by_course"] = absences_by_course
            result_students.append(student)

        return {
            "specialty_semesters": specialty_semesters,
            "courses": courses,
            "students": result_students
        }
    except Exception as e:
        print(f"❌ ERROR in admin_absences_overview: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Internal error: {str(e)}")
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/print/departments")
async def admin_print_departments(current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            raise HTTPException(400, "No active semester configured")
        cursor.execute("""
            SELECT DISTINCT 
                cu.specialty_name, 
                cu.semester, 
                cu.department,
                s.name AS semester_name   -- το πλήρες όνομα (π.χ. 2026α)
            FROM course_units cu
            JOIN semesters s ON cu.semester_id = s.id
            WHERE cu.semester_id = %s
            ORDER BY cu.specialty_name, cu.semester, cu.department
        """, (semester_id,))
        rows = cursor.fetchall()
        departments = []
        for row in rows:
            display = f"{row['specialty_name']} {row['department']}"
            departments.append({
                "specialty_name": row['specialty_name'],
                "semester": row['semester'],          # το γράμμα (Α, Β, Γ, Δ) – για άλλες χρήσεις
                "department": row['department'],
                "semester_name": row['semester_name'], # το πλήρες όνομα (π.χ. 2026α)
                "display": display
            })
        return {"departments": departments}
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/print/groups")
async def admin_print_groups(
    specialty_name: str,
    semester: str,
    department: str,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        # 1. Φόρτωση όλων των course_units για το συγκεκριμένο τμήμα
        cursor.execute("""
            SELECT id, lesson_name, type_indicator, team
            FROM course_units
            WHERE specialty_name = %s AND semester = %s AND department = %s AND semester_id = %s
            ORDER BY lesson_name, team
        """, (specialty_name, semester, department, semester_id))
        course_units = cursor.fetchall()
        
        groups = []
        for cu in course_units:
            # 2. Εκπαιδευτές
            cursor.execute("""
                SELECT CONCAT(t.surname, ' ', t.name) AS teacher_name
                FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                WHERE ca.course_unit_id = %s
                ORDER BY t.surname, t.name
            """, (cu['id'],))
            teachers = [row['teacher_name'] for row in cursor.fetchall()]
            
            # 3. Μαθητές (με φίλτρο ομάδας αν υπάρχει)
            if cu['team']:
                cursor.execute("""
                    SELECT s.id, s.amk, s.surname, s.name
                    FROM students s
                    JOIN student_teams st ON s.id = st.student_id
                    WHERE s.specialty_name = %s AND s.semester = %s AND s.department = %s
                      AND s.semester_id = %s
                      AND st.team = %s
                    ORDER BY s.surname, s.name
                """, (specialty_name, semester, department, semester_id, cu['team']))
            else:
                cursor.execute("""
                    SELECT s.id, s.amk, s.surname, s.name
                    FROM students s
                    WHERE s.specialty_name = %s AND s.semester = %s AND s.department = %s
                      AND s.semester_id = %s
                    ORDER BY s.surname, s.name
                """, (specialty_name, semester, department, semester_id))
            students = cursor.fetchall()
            
            # 4. Ημερομηνίες μαθημάτων και ώρες
            cursor.execute("""
                SELECT lesson_date, hours
                FROM lessons
                WHERE course_unit_id = %s
                ORDER BY lesson_date
            """, (cu['id'],))
            lessons = cursor.fetchall()
            lesson_dates = [row['lesson_date'].isoformat() for row in lessons]
            lesson_hours = {row['lesson_date'].isoformat(): row['hours'] for row in lessons}
            
            # 5. Απουσίες ανά μαθητή
            student_absences = {}
            for student in students:
                cursor.execute("""
                    SELECT l.lesson_date, a.hours_absent
                    FROM absences a
                    JOIN lessons l ON a.lesson_id = l.id
                    WHERE a.student_id = %s AND l.course_unit_id = %s
                """, (student['id'], cu['id']))
                abs_rows = cursor.fetchall()
                abs_dict = {row['lesson_date'].isoformat(): row['hours_absent'] for row in abs_rows}
                student_absences[student['id']] = abs_dict
            
            # 6. Δημιουργία group (μόνο αν υπάρχουν μαθητές)
            if students:
                group = {
                    "lesson_name": cu['lesson_name'],
                    "type_indicator": cu['type_indicator'],
                    "team": cu['team'] or None,
                    "specialty_dept": f"{specialty_name} {department}",
                    "teachers": teachers,
                    "students": [
                        {
                            "amk": s['amk'],
                            "surname": s['surname'],
                            "name": s['name'],
                            "absences": student_absences.get(s['id'], {})
                        }
                        for s in students
                    ],
                    "lesson_dates": lesson_dates,
                    "lesson_hours": lesson_hours
                }
                groups.append(group)
        
        return {"groups": groups}
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/print/syllabus")
async def admin_print_syllabus(
    specialty_name: str,
    semester: str,
    department: str,
    current_user = Depends(require_role("admin"))
):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        # 1. Φόρτωση όλων των course_units για το συγκεκριμένο τμήμα
        cursor.execute("""
            SELECT id, lesson_name, type_indicator, team
            FROM course_units
            WHERE specialty_name = %s AND semester = %s AND department = %s AND semester_id = %s
            ORDER BY lesson_name, team
        """, (specialty_name, semester, department, semester_id))
        course_units = cursor.fetchall()
        
        groups = []
        for cu in course_units:
            # 2. Εκπαιδευτές
            cursor.execute("""
                SELECT CONCAT(t.surname, ' ', t.name) AS teacher_name
                FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                WHERE ca.course_unit_id = %s
                ORDER BY t.surname, t.name
            """, (cu['id'],))
            teachers = [row['teacher_name'] for row in cursor.fetchall()]
            
            # 3. Lessons (ημερομηνία, ώρες, syllabus)
            cursor.execute("""
                SELECT lesson_date, hours, syllabus_content
                FROM lessons
                WHERE course_unit_id = %s
                ORDER BY lesson_date DESC
            """, (cu['id'],))
            lessons = cursor.fetchall()
            
            group = {
                "lesson_name": cu['lesson_name'],
                "type_indicator": cu['type_indicator'],
                "team": cu['team'] or None,
                "specialty_dept": f"{specialty_name} {department}",
                "teachers": teachers,
                "lessons": [
                    {
                        "lesson_date": row['lesson_date'].isoformat(),
                        "hours": row['hours'],
                        "syllabus": row['syllabus_content'] or ''
                    }
                    for row in lessons
                ]
            }
            groups.append(group)
        
        return {"groups": groups}
    finally:
        cursor.close()
        conn.close()

# -------------------- SETTINGS ENDPOINTS --------------------
@app.get("/admin/settings")
async def admin_get_settings(current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'email_signature'")
        sig_row = cursor.fetchone()
        signature = sig_row["setting_value"] if sig_row else ""
        cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'default_to_recipient'")
        to_row = cursor.fetchone()
        to_recipient = to_row["setting_value"] if to_row else ""
        cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'default_recipients'")
        rec_row = cursor.fetchone()
        default_recipients = rec_row["setting_value"] if rec_row else ""
        cursor.execute("SELECT id, name, email, smtp_host, smtp_port, username, is_active FROM email_senders")
        senders = cursor.fetchall()
        for s in senders:
            s.pop("password", None)
        cursor.execute("SELECT id, username FROM users WHERE role = 'admin'")
        admins = cursor.fetchall()
        cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'default_cc_recipients'")
        cc_row = cursor.fetchone()
        cc_recipients = cc_row["setting_value"] if cc_row else ""
        cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'unit_name'")
        unit_name_row = cursor.fetchone()
        unit_name = unit_name_row["setting_value"] if unit_name_row else "ΣΑΕΚ"
        return {"signature": signature, "default_recipients": default_recipients, "senders": senders, "admins": admins, "unit_name": unit_name, "to_recipient": to_recipient, "cc_recipients": cc_recipients}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/settings/unit-name")
async def admin_update_unit_name(data: dict, current_user = Depends(require_role("admin"))):
    unit_name = data.get("unit_name", "").strip()
    if not unit_name:
        raise HTTPException(400, "Unit name cannot be empty")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO settings (setting_key, setting_value) 
            VALUES ('unit_name', %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """, (unit_name,))
        conn.commit()
        return {"message": "Unit name updated"}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/settings/to-recipient")
async def admin_update_to_recipient(data: dict, current_user = Depends(require_role("admin"))):
    to_recipient = data.get("to_recipient", "").strip()
    if not to_recipient:
        raise HTTPException(400, "To recipient is required")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO settings (setting_key, setting_value) 
            VALUES ('default_to_recipient', %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """, (to_recipient,))
        conn.commit()
        return {"message": "To recipient updated"}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/settings/default-recipients")
async def admin_update_default_recipients(data: dict, current_user = Depends(require_role("admin"))):
    default_recipients = data.get("default_recipients", "").strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE settings SET setting_value = %s WHERE setting_key = 'default_recipients'", (default_recipients,))
        if cursor.rowcount == 0:
            cursor.execute("INSERT INTO settings (setting_key, setting_value) VALUES ('default_recipients', %s)", (default_recipients,))
        conn.commit()
        return {"message": "Default recipients updated"}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/settings/signature")
async def admin_update_signature(data: dict, current_user = Depends(require_role("admin"))):
    signature = data.get("signature", "").strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO settings (setting_key, setting_value) 
            VALUES ('email_signature', %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """, (signature,))
        conn.commit()
        return {"message": "Signature updated"}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/settings/senders")
async def admin_add_sender(data: dict, current_user = Depends(require_role("admin"))):
    name = data.get("name")
    email = data.get("email")
    smtp_host = data.get("smtp_host", "smtp.gmail.com")
    smtp_port = data.get("smtp_port", 587)
    username = data.get("username")
    password = data.get("password")
    if not name or not email or not username or not password:
        raise HTTPException(400, "Missing required fields")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO email_senders (name, email, smtp_host, smtp_port, username, password)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (name, email, smtp_host, smtp_port, username, password))
        conn.commit()
        return {"message": "Sender added", "id": cursor.lastrowid}
    except mysql.connector.IntegrityError:
        raise HTTPException(400, "Sender with this email already exists")
    finally:
        cursor.close()
        conn.close()

@app.delete("/admin/settings/senders/{sender_id}")
async def admin_delete_sender(sender_id: int, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM email_senders WHERE id = %s", (sender_id,))
        conn.commit()
        return {"message": "Sender deleted"}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/settings/senders/{sender_id}")
async def admin_update_sender(sender_id: int, data: dict, current_user = Depends(require_role("admin"))):
    name = data.get("name")
    email = data.get("email")
    smtp_host = data.get("smtp_host")
    smtp_port = data.get("smtp_port")
    username = data.get("username")
    password = data.get("password")
    is_active = data.get("is_active")
    
    if not any([name, email, smtp_host, smtp_port, username, password, is_active is not None]):
        raise HTTPException(400, "No fields to update")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        updates = []
        values = []
        if name:
            updates.append("name = %s"); values.append(name)
        if email:
            updates.append("email = %s"); values.append(email)
        if smtp_host:
            updates.append("smtp_host = %s"); values.append(smtp_host)
        if smtp_port:
            updates.append("smtp_port = %s"); values.append(smtp_port)
        if username:
            updates.append("username = %s"); values.append(username)
        if password:
            updates.append("password = %s"); values.append(password)
        if is_active is not None:
            updates.append("is_active = %s"); values.append(is_active)
        values.append(sender_id)
        cursor.execute(f"UPDATE email_senders SET {', '.join(updates)} WHERE id = %s", values)
        conn.commit()
        return {"message": "Sender updated"}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/settings/admins")
async def admin_add_admin(data: dict, current_user = Depends(require_role("admin"))):
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    hashed = hash_password(password)   # χρησιμοποιεί την ίδια συνάρτηση που έχεις ήδη
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, 'admin')", (username, hashed))
        conn.commit()
        return {"message": "Admin user added", "id": cursor.lastrowid}
    except mysql.connector.IntegrityError:
        raise HTTPException(400, "Username already exists")
    finally:
        cursor.close()
        conn.close()

@app.delete("/admin/settings/admins/{admin_id}")
async def admin_delete_admin(admin_id: int, current_user = Depends(require_role("admin"))):
    if admin_id == current_user["user_id"]:
        raise HTTPException(400, "You cannot delete your own admin account")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM users WHERE id = %s AND role = 'admin'", (admin_id,))
        conn.commit()
        return {"message": "Admin user deleted"}
    finally:
        cursor.close()
        conn.close()

# -------------------- SEMESTER ENDPOINTS --------------------
@app.get("/admin/semesters/active")
async def admin_get_active_semester(current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'active_semester_id'")
        row = cursor.fetchone()
        if not row:
            return {"active_semester_id": None}
        active_id = int(row["setting_value"])
        cursor.execute("SELECT id, name FROM semesters WHERE id = %s", (active_id,))
        semester = cursor.fetchone()
        return {"active_semester_id": active_id, "active_semester": semester}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/semesters/active")
async def admin_set_active_semester(data: dict, current_user = Depends(require_role("admin"))):
    semester_id = data.get("semester_id")
    if not semester_id:
        raise HTTPException(400, "semester_id required")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Έλεγχος ύπαρξης
        cursor.execute("SELECT id FROM semesters WHERE id = %s", (semester_id,))
        if not cursor.fetchone():
            raise HTTPException(404, "Semester not found")
        # Απενεργοποίησε όλα
        cursor.execute("UPDATE semesters SET is_active = FALSE")
        # Ενεργοποίησε το επιλεγμένο
        cursor.execute("UPDATE semesters SET is_active = TRUE WHERE id = %s", (semester_id,))
        # Αποθήκευσε στο settings
        cursor.execute("""
            INSERT INTO settings (setting_key, setting_value) 
            VALUES ('active_semester_id', %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """, (semester_id,))
        conn.commit()
        return {"message": "Active semester updated"}
    finally:
        cursor.close()
        conn.close()

@app.get("/admin/semesters")
async def admin_list_semesters(current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name, is_active, start_date, end_date FROM semesters ORDER BY id DESC")
        semesters = cursor.fetchall()
        return {"semesters": semesters}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/semesters")
async def admin_add_semester(data: dict, current_user = Depends(require_role("admin"))):
    name = data.get("name", "").strip()
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    if not name:
        raise HTTPException(400, "Semester name required")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO semesters (name, start_date, end_date) VALUES (%s, %s, %s)",
            (name, start_date, end_date)
        )
        conn.commit()
        return {"message": "Semester added", "id": cursor.lastrowid}
    finally:
        cursor.close()
        conn.close()

@app.put("/admin/semesters/{semester_id}")
async def admin_update_semester(semester_id: int, data: dict, current_user = Depends(require_role("admin"))):
    name = data.get("name")
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    is_active = data.get("is_active")
    if not any([name, start_date, end_date, is_active is not None]):
        raise HTTPException(400, "No fields to update")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        updates = []
        values = []
        if name:
            updates.append("name = %s"); values.append(name)
        if start_date:
            updates.append("start_date = %s"); values.append(start_date)
        if end_date:
            updates.append("end_date = %s"); values.append(end_date)
        if is_active is not None:
            updates.append("is_active = %s"); values.append(is_active)
            # Αν το ζητούμενο είναι να γίνει ενεργό, απενεργοποίησε όλα τα άλλα
            if is_active:
                cursor.execute("UPDATE semesters SET is_active = FALSE")
        values.append(semester_id)
        cursor.execute(f"UPDATE semesters SET {', '.join(updates)} WHERE id = %s", values)
        # Αν το is_active έγινε True, ενημέρωσε και το settings
        if is_active:
            cursor.execute("""
                INSERT INTO settings (setting_key, setting_value) 
                VALUES ('active_semester_id', %s)
                ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
            """, (semester_id,))
        conn.commit()
        return {"message": "Semester updated"}
    finally:
        cursor.close()
        conn.close()

@app.delete("/admin/semesters/{semester_id}")
async def admin_delete_semester(semester_id: int, current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Έλεγχος αν υπάρχουν δεδομένα που συνδέονται με αυτό το εξάμηνο
        cursor.execute("SELECT COUNT(*) FROM teachers WHERE semester_id = %s", (semester_id,))
        if cursor.fetchone()[0] > 0:
            raise HTTPException(400, "Cannot delete: there are teachers linked to this semester")
        cursor.execute("SELECT COUNT(*) FROM students WHERE semester_id = %s", (semester_id,))
        if cursor.fetchone()[0] > 0:
            raise HTTPException(400, "Cannot delete: there are students linked to this semester")
        cursor.execute("SELECT COUNT(*) FROM course_units WHERE semester_id = %s", (semester_id,))
        if cursor.fetchone()[0] > 0:
            raise HTTPException(400, "Cannot delete: there are course units linked to this semester")
        cursor.execute("DELETE FROM semesters WHERE id = %s", (semester_id,))
        conn.commit()
        return {"message": "Semester deleted"}
    finally:
        cursor.close()
        conn.close()

# -------------------- BULK EMAIL --------------------
@app.post("/admin/teachers/emails")
async def admin_get_teachers_emails(data: dict, current_user = Depends(require_role("admin"))):
    teacher_ids = data.get("teacher_ids", [])
    if not teacher_ids:
        raise HTTPException(400, "No teacher IDs provided")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        placeholders = ','.join(['%s'] * len(teacher_ids))
        query = f"SELECT mail FROM teachers WHERE id IN ({placeholders})"
        cursor.execute(query, teacher_ids)
        emails = [row["mail"] for row in cursor.fetchall() if row.get("mail")]
        return {"emails": emails}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/course-units/students-emails")
async def admin_get_course_students_emails(data: dict, current_user = Depends(require_role("admin"))):
    course_unit_ids = data.get("course_unit_ids", [])
    if not course_unit_ids:
        raise HTTPException(400, "No course unit IDs provided")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        emails_set = set()
        for cu_id in course_unit_ids:
            cursor.execute("SELECT specialty_name, semester, department, team FROM course_units WHERE id = %s", (cu_id,))
            course = cursor.fetchone()
            if not course:
                continue
            specialty = course['specialty_name']
            sem = course['semester']
            dept = course['department']
            team_target = course['team'] or ""
            query = """
                SELECT mail FROM students
                WHERE specialty_name = %s AND semester = %s AND department = %s
            """
            params = [specialty, sem, dept]
            if team_target.strip():
                query += " AND id IN (SELECT student_id FROM student_teams WHERE team = %s)"
                params.append(team_target)
            cursor.execute(query, params)
            rows = cursor.fetchall()
            for row in rows:
                if row['mail'] and row['mail'].strip():
                    emails_set.add(row['mail'])
        return {"emails": list(emails_set)}
    finally:
        cursor.close()
        conn.close()

@app.post("/admin/send-bulk-email")
async def send_bulk_email(data: dict, current_user = Depends(require_role("admin"))):
    sender_name = data.get("sender")  # "no-reply" or "admin"
    subject = data.get("subject", "").strip()
    message = data.get("message", "").strip()
    recipients = data.get("recipients", [])
    cc_from_frontend = data.get("cc_recipients", [])

    if not subject or not message:
        raise HTTPException(400, "Θέμα και μήνυμα απαιτούνται")
    if not recipients:
        raise HTTPException(400, "Δεν υπάρχουν παραλήπτες")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Λήψη sender
    cursor.execute("SELECT * FROM email_senders WHERE name = %s AND is_active = TRUE", (sender_name,))
    sender = cursor.fetchone()
    if not sender:
        cursor.close()
        conn.close()
        raise HTTPException(404, f"Δεν βρέθηκε ενεργός λογαριασμός με όνομα '{sender_name}'")

    # Λήψη αποθηκευμένων CC
    cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'default_cc_recipients'")
    cc_row = cursor.fetchone()
    default_cc = cc_row["setting_value"].split(",") if cc_row and cc_row["setting_value"] else []
    default_cc = [email.strip() for email in default_cc if email.strip()]

    # Λήψη του "Προς" παραλήπτη
    cursor.execute("SELECT setting_value FROM settings WHERE setting_key = 'default_to_recipient'")
    to_row = cursor.fetchone()
    to_recipient = to_row["setting_value"].strip() if to_row and to_row["setting_value"] else "admin"
    
    cursor.close()
    conn.close()

    # Συνδυασμός CC
    all_cc = list(set(default_cc + cc_from_frontend))

    msg = MIMEText(message)
    msg['Subject'] = subject
    msg['From'] = sender["email"]
    msg['To'] = to_recipient
    if all_cc:
        msg['Cc'] = ", ".join(all_cc)
    msg['Bcc'] = ", ".join(recipients)

    try:
        with smtplib.SMTP(sender["smtp_host"], sender["smtp_port"]) as server:
            server.starttls()
            server.login(sender["username"], sender["password"])
            server.send_message(msg)
        return {"message": f"Email στάλθηκε σε {len(recipients)} BCC παραλήπτες" + (f" και CC σε {len(all_cc)}" if all_cc else "")}
    except Exception as e:
        print(f"Email error: {e}")
        raise HTTPException(500, f"Αποτυχία αποστολής email: {str(e)}")

@app.put("/admin/settings/cc-recipients")
async def admin_update_cc_recipients(data: dict, current_user = Depends(require_role("admin"))):
    cc_recipients = data.get("cc_recipients", "").strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO settings (setting_key, setting_value) 
            VALUES ('default_cc_recipients', %s)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """, (cc_recipients,))
        conn.commit()
        return {"message": "CC recipients updated"}
    finally:
        cursor.close()
        conn.close()

# -------------------- ADDITIONAL VIEWS --------------------
@app.get("/course-units")
async def list_course_units(current_user = Depends(require_role("admin", "instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            return {"course_units": []}
        cursor.execute("""
            SELECT id, specialty_name, semester, department, team, lesson_name, type_indicator, classroom
            FROM course_units
            WHERE semester_id = %s
            ORDER BY specialty_name, semester, department, team
        """, (semester_id,))
        units = cursor.fetchall()
        return {"course_units": units}
    finally:
        cursor.close()
        conn.close()

@app.get("/course-units/{course_unit_id}/today-lesson")
async def get_today_lesson(course_unit_id: int, current_user = Depends(require_role("instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT 1 FROM course_assignments ca JOIN teachers t ON ca.teacher_id = t.id WHERE t.user_id = %s AND ca.course_unit_id = %s",
                       (current_user["user_id"], course_unit_id))
        if not cursor.fetchone():
            raise HTTPException(403, "Δεν διδάσκετε αυτό το μάθημα")
        today = date.today()
        cursor.execute("SELECT id, hours, syllabus_content FROM lessons WHERE course_unit_id = %s AND lesson_date = %s", (course_unit_id, today))
        lesson = cursor.fetchone()
        if not lesson:
            return {"exists": False}
        cursor.execute("SELECT student_id, hours_absent FROM absences WHERE lesson_id = %s", (lesson["id"],))
        absences = cursor.fetchall()
        lesson["absences"] = absences
        lesson["exists"] = True
        return lesson
    finally:
        cursor.close()
        conn.close()

@app.get("/assignments")
async def list_assignments(current_user = Depends(require_role("admin", "instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            return {"assignments": []}
        
        if current_user["user_role"] == "admin":
            cursor.execute("""
                SELECT t.id AS teacher_id, t.surname, t.name, t.mail,
                       cu.id AS course_unit_id, cu.specialty_name, cu.semester, cu.department, cu.team, cu.lesson_name, cu.type_indicator,
                       cu.weekly_hours
                FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                JOIN course_units cu ON ca.course_unit_id = cu.id
                WHERE cu.semester_id = %s
                ORDER BY t.surname, cu.specialty_name, cu.lesson_name
            """, (semester_id,))
        else:  # instructor
            cursor.execute("""
                SELECT t.id AS teacher_id, t.surname, t.name, t.mail,
                       cu.id AS course_unit_id, cu.specialty_name, cu.semester, cu.department, cu.team, cu.lesson_name, cu.type_indicator,
                       cu.weekly_hours
                FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                JOIN course_units cu ON ca.course_unit_id = cu.id
                WHERE t.user_id = %s AND cu.semester_id = %s
                ORDER BY t.surname, cu.specialty_name, cu.lesson_name
            """, (current_user["user_id"], semester_id))
        assignments = cursor.fetchall()
        return {"assignments": assignments}
    finally:
        cursor.close()
        conn.close()

@app.get("/students")
async def list_students(current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        semester_id = get_active_semester_id()
        if semester_id is None:
            return {"students": []}
        
        cursor.execute("""
            SELECT id, amk, surname, name, father_name, specialty_name, semester, department
            FROM students
            WHERE semester_id = %s
            ORDER BY specialty_name, semester, department, surname, name
        """, (semester_id,))
        students = cursor.fetchall()
        return {"students": students}
    finally:
        cursor.close()
        conn.close()

@app.get("/student-teams")
async def list_student_teams(current_user = Depends(require_role("admin"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            return {"student_teams": []}
        cursor.execute("""
            SELECT st.student_id, s.amk, s.surname, s.name, st.team
            FROM student_teams st
            JOIN students s ON st.student_id = s.id
            WHERE s.semester_id = %s
            ORDER BY s.surname, s.name, st.team
        """, (active_semester_id,))
        teams = cursor.fetchall()
        return {"student_teams": teams}
    finally:
        cursor.close()
        conn.close()

@app.get("/course-units/{course_unit_id}/students")
async def list_students_for_course(course_unit_id: int, current_user = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 1. Βασικός έλεγχος ρόλου
        if current_user["user_role"] not in ("admin", "instructor"):
            raise HTTPException(403, "Access denied")
        
        # 2. Βρες το course_unit και ελέγξε ότι ανήκει στο ενεργό εξάμηνο
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        cursor.execute("""
            SELECT id, specialty_name, semester, department, team, semester_id
            FROM course_units WHERE id = %s
        """, (course_unit_id,))
        course = cursor.fetchone()
        if not course:
            raise HTTPException(404, "Course unit not found")
        
        if course["semester_id"] != active_semester_id:
            raise HTTPException(404, "Course unit not available in the current semester")
        
        specialty = course['specialty_name']
        sem = course['semester']
        dept = course['department']
        team_target = course['team'] or ""
        
        # 3. Αν είναι instructor, ελέγχουμε ότι διδάσκει αυτό το μάθημα (στο ίδιο εξάμηνο)
        if current_user["user_role"] == "instructor":
            cursor.execute("""
                SELECT 1 FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                WHERE t.user_id = %s AND ca.course_unit_id = %s
            """, (current_user["user_id"], course_unit_id))
            if not cursor.fetchone():
                raise HTTPException(403, "You do not teach this course")
        
        # 4. Διαφορετικά πεδία ανάλογα με τον ρόλο, και φιλτράρισμα μαθητών που ανήκουν στο ίδιο εξάμηνο
        if current_user["user_role"] == "admin":
            fields = "id, amk, surname, name, father_name, mother_name, mail, phone"
        else:
            fields = "id, amk, surname, name"
        
        query = f"""
            SELECT {fields}
            FROM students
            WHERE specialty_name = %s AND semester = %s AND department = %s
              AND semester_id = %s
        """
        params = [specialty, sem, dept, active_semester_id]
        
        if team_target.strip():
            query += " AND id IN (SELECT student_id FROM student_teams WHERE team = %s)"
            params.append(team_target)
        
        query += " ORDER BY surname, name"
        cursor.execute(query, params)
        students = cursor.fetchall()
        return {
            "course_unit_id": course_unit_id,
            "specialty_name": specialty,
            "semester": sem,
            "department": dept,
            "team": team_target if team_target.strip() else None,
            "students": students
        }
    finally:
        cursor.close()
        conn.close()

@app.get("/course-units/{course_unit_id}/total-hours")
async def get_total_teaching_hours(course_unit_id: int, current_user = Depends(get_current_user)):
    if current_user["user_role"] not in ("admin", "instructor"):
        raise HTTPException(403, "Access denied")
    
    # Βρες το ενεργό εξάμηνο
    active_semester_id = get_active_semester_id()
    if active_semester_id is None:
        return {"course_unit_id": course_unit_id, "total_hours_taught": 0}
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Αν είναι instructor, έλεγχος ότι διδάσκει το μάθημα ΚΑΙ ότι το μάθημα ανήκει στο ενεργό εξάμηνο
        if current_user["user_role"] == "instructor":
            cursor.execute("""
                SELECT 1 FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                JOIN course_units cu ON ca.course_unit_id = cu.id
                WHERE t.user_id = %s AND ca.course_unit_id = %s AND cu.semester_id = %s
            """, (current_user["user_id"], course_unit_id, active_semester_id))
            if not cursor.fetchone():
                raise HTTPException(403, "You do not have access to this course")
        
        # Υπολογισμός ωρών μόνο για μαθήματα του ενεργού εξαμήνου (ήδη το course_unit ανήκει στο active_semester_id)
        cursor.execute("""
            SELECT SUM(hours) AS total 
            FROM lessons l
            JOIN course_units cu ON l.course_unit_id = cu.id
            WHERE l.course_unit_id = %s AND cu.semester_id = %s
        """, (course_unit_id, active_semester_id))
        result = cursor.fetchone()
        return {"course_unit_id": course_unit_id, "total_hours_taught": result['total'] or 0}
    finally:
        cursor.close()
        conn.close()

@app.get("/course-units/{course_unit_id}/lessons")
async def get_lessons_for_course(course_unit_id: int, current_user = Depends(require_role("admin", "instructor"))):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        active_semester_id = get_active_semester_id()
        if active_semester_id is None:
            raise HTTPException(400, "No active semester configured")
        
        # 1. Βρες το course_unit
        cursor.execute("SELECT id, specialty_name, semester, department, semester_id FROM course_units WHERE id = %s", (course_unit_id,))
        course = cursor.fetchone()
        if not course:
            raise HTTPException(404, "Course unit not found")
        if course["semester_id"] != active_semester_id:
            raise HTTPException(404, "Course unit not available in the current semester")
        
        # 2. Έλεγχος πρόσβασης
        if current_user["user_role"] == "instructor":
            # Βρες τον teacher_id για τον τρέχοντα χρήστη στο ενεργό εξάμηνο
            cursor.execute("SELECT id FROM teachers WHERE user_id = %s AND semester_id = %s", (current_user["user_id"], active_semester_id))
            teacher = cursor.fetchone()
            if not teacher:
                raise HTTPException(403, "Δεν βρέθηκε εκπαιδευτής για τον λογαριασμό σας στο τρέχον εξάμηνο")
            teacher_id = teacher["id"]
            
            # Έλεγχος: υπάρχει ανάθεση σε οποιοδήποτε μάθημα με το ίδιο specialty_name, semester, department;
            cursor.execute("""
                SELECT 1 FROM course_assignments ca
                JOIN course_units cu ON ca.course_unit_id = cu.id
                WHERE ca.teacher_id = %s
                  AND cu.specialty_name = %s
                  AND cu.semester = %s
                  AND cu.department = %s
                  AND cu.semester_id = %s
                LIMIT 1
            """, (teacher_id, course["specialty_name"], course["semester"], course["department"], active_semester_id))
            if not cursor.fetchone():
                raise HTTPException(403, "Δεν έχετε πρόσβαση σε αυτό το τμήμα")
        
        # 3. Επιστροφή των lessons
        cursor.execute("""
            SELECT id, lesson_date, hours, syllabus_content, created_at
            FROM lessons
            WHERE course_unit_id = %s
            ORDER BY lesson_date DESC
        """, (course_unit_id,))
        lessons = cursor.fetchall()
        return {"course_unit_id": course_unit_id, "lessons": lessons}
    finally:
        cursor.close()
        conn.close()

@app.get("/course-units/{course_unit_id}/student-absences")
async def get_student_absences(course_unit_id: int, current_user = Depends(get_current_user)):
    if current_user["user_role"] not in ("admin", "instructor"):
        raise HTTPException(403, "Access denied")
    
    active_semester_id = get_active_semester_id()
    if active_semester_id is None:
        return {"course_unit_id": course_unit_id, "absences": []}
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Αν είναι instructor, έλεγχος ότι διδάσκει το μάθημα στο ενεργό εξάμηνο
        if current_user["user_role"] == "instructor":
            cursor.execute("""
                SELECT 1 FROM course_assignments ca
                JOIN teachers t ON ca.teacher_id = t.id
                JOIN course_units cu ON ca.course_unit_id = cu.id
                WHERE t.user_id = %s AND ca.course_unit_id = %s AND cu.semester_id = %s
            """, (current_user["user_id"], course_unit_id, active_semester_id))
            if not cursor.fetchone():
                raise HTTPException(403, "You do not have access to this course")
        
        # Υπολογισμός απουσιών μόνο για μαθήματα του ενεργού εξαμήνου
        cursor.execute("""
            SELECT s.id, s.amk, s.surname, s.name, SUM(a.hours_absent) AS total_absent_hours
            FROM absences a
            JOIN lessons l ON a.lesson_id = l.id
            JOIN students s ON a.student_id = s.id
            JOIN course_units cu ON l.course_unit_id = cu.id
            WHERE l.course_unit_id = %s AND cu.semester_id = %s
            GROUP BY s.id
            ORDER BY s.surname, s.name
        """, (course_unit_id, active_semester_id))
        absences = cursor.fetchall()
        return {"course_unit_id": course_unit_id, "absences": absences}
    finally:
        cursor.close()
        conn.close()

@app.get("/students/{student_id}/absences")
async def get_student_absence_history(student_id: int, current_user = Depends(get_current_user)):
    if current_user["user_role"] == "student" and current_user["user_id"] != student_id:
        raise HTTPException(403, "Access denied")
    if current_user["user_role"] not in ("admin", "student"):
        raise HTTPException(403, "Only admin or the student can view absence history")
    
    active_semester_id = get_active_semester_id()
    if active_semester_id is None:
        return {"student_id": student_id, "absences": []}
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM students WHERE id = %s", (student_id,))
        if not cursor.fetchone():
            raise HTTPException(404, "Student not found")
        
        cursor.execute("""
            SELECT 
                l.lesson_date,
                cu.lesson_name,
                cu.specialty_name,
                cu.semester,
                cu.department,
                COALESCE(cu.team, '') AS team,
                COALESCE(l.hours, 1) AS total_lesson_hours,
                a.hours_absent
            FROM absences a
            JOIN lessons l ON a.lesson_id = l.id
            JOIN course_units cu ON l.course_unit_id = cu.id
            WHERE a.student_id = %s AND cu.semester_id = %s
            ORDER BY l.lesson_date DESC
        """, (student_id, active_semester_id))
        absences = cursor.fetchall()
        return {"student_id": student_id, "absences": absences}
    finally:
        cursor.close()
        conn.close()

@app.get("/student/{student_id}/summary")
async def get_student_absences_summary(student_id: int, current_user = Depends(get_current_user)):
    if current_user["user_role"] == "student" and current_user["user_id"] != student_id:
        raise HTTPException(403, "Access denied")
    if current_user["user_role"] not in ("admin", "student"):
        raise HTTPException(403, "Only admin or the student can view absences summary")

    active_semester_id = get_active_semester_id()
    if active_semester_id is None:
        raise HTTPException(400, "No active semester configured")
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, amk, surname, name, specialty_name, semester, department
            FROM students WHERE id = %s
        """, (student_id,))
        student = cursor.fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

        specialty = student['specialty_name']
        sem = student['semester']
        dept = student['department']

        cursor.execute("SELECT team FROM student_teams WHERE student_id = %s", (student_id,))
        teams_rows = cursor.fetchall()
        student_teams = [row['team'] for row in teams_rows]

        # Βρες όλα τα course_units που αντιστοιχούν στον μαθητή ΚΑΙ ανήκουν στο ενεργό εξάμηνο
        query = """
            SELECT id, lesson_name, team, type_indicator, classroom
            FROM course_units
            WHERE specialty_name = %s AND semester = %s AND department = %s
              AND semester_id = %s
              AND (team = '' OR team IS NULL)
        """
        params = [specialty, sem, dept, active_semester_id]
        if student_teams:
            placeholders = ','.join(['%s'] * len(student_teams))
            query += f" OR (team IN ({placeholders}) AND semester_id = %s)"
            params.extend(student_teams + [active_semester_id])

        cursor.execute(query, params)
        courses = cursor.fetchall()

        result_courses = []
        for course in courses:
            course_id = course['id']
            cursor.execute("SELECT COALESCE(SUM(hours), 0) AS total_hours FROM lessons WHERE course_unit_id = %s", (course_id,))
            total_hours = cursor.fetchone()['total_hours']
            cursor.execute("""
                SELECT COALESCE(SUM(a.hours_absent), 0) AS absent_hours
                FROM absences a
                JOIN lessons l ON a.lesson_id = l.id
                WHERE l.course_unit_id = %s AND a.student_id = %s
            """, (course_id, student_id))
            absent_hours = cursor.fetchone()['absent_hours']
            cursor.execute("""
                SELECT l.lesson_date, l.hours AS lesson_hours, a.hours_absent
                FROM absences a
                JOIN lessons l ON a.lesson_id = l.id
                WHERE l.course_unit_id = %s AND a.student_id = %s
                ORDER BY l.lesson_date DESC
            """, (course_id, student_id))
            details = cursor.fetchall()
            result_courses.append({
                "course_id": course_id,
                "lesson_name": course['lesson_name'],
                "team": course['team'] or "χωρίς ομάδα",
                "type_indicator": course['type_indicator'],
                "classroom": course['classroom'],
                "total_teaching_hours": total_hours,
                "total_absent_hours": absent_hours,
                "absences_details": details
            })

        return {
            "student": {
                "id": student['id'],
                "amk": student['amk'],
                "surname": student['surname'],
                "name": student['name'],
                "specialty_name": specialty,
                "semester": sem,
                "department": dept,
                "teams": student_teams if student_teams else ["χωρίς ομάδα"]
            },
            "courses": result_courses
        }
    finally:
        cursor.close()
        conn.close()

# -------------------- ADMIN LOGIN --------------------
@app.post("/admin/login")
async def admin_login(request: Request, response: Response):
    data = await request.json()
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        raise HTTPException(400, "Email and password required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, role, password FROM users WHERE username = %s", (email,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user or user["role"] != "admin":
        raise HTTPException(401, "Invalid admin credentials")

    stored = user["password"]

    # Έλεγχος αν είναι plaintext (δεν ξεκινάει με $2b$ και μήκος < 60)
    if not stored.startswith('$2b$') and len(stored) < 60:
        # Plaintext σύγκριση
        if stored != password:
            raise HTTPException(401, "Invalid admin credentials")
        # Migration σε bcrypt hash
        new_hash = hash_password(password)
        conn2 = get_db_connection()
        cursor2 = conn2.cursor()
        cursor2.execute("UPDATE users SET password = %s WHERE id = %s", (new_hash, user["id"]))
        conn2.commit()
        cursor2.close()
        conn2.close()
    else:
        # Κανονική επαλήθευση με bcrypt
        if not verify_password(password, stored):
            raise HTTPException(401, "Invalid admin credentials")

    # Δημιουργία session token
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(days=7)

    conn3 = get_db_connection()
    cursor3 = conn3.cursor()
    cursor3.execute("""
        INSERT INTO sessions (token, user_id, user_role, expires_at)
        VALUES (%s, %s, %s, %s)
    """, (token, user["id"], "admin", expires))
    conn3.commit()
    cursor3.close()
    conn3.close()

    resp = JSONResponse(content={"message": "Admin login successful"})
    
    cookie_settings = get_cookie_settings(request)
    resp.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=cookie_settings["secure"],
        samesite="lax",
        domain=cookie_settings["domain"],
        max_age=7*24*3600
    )
    return resp

# -------------------- HEALTH --------------------
@app.get("/health")
def health():
    return {"status": "ok"}
