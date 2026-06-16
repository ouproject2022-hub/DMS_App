# ===============================
# School Data Entry Web App
# Tech: Python Flask + Supabase PostgreSQL + Google Drive OAuth Image Upload
# ===============================

from flask import Flask, render_template, request, redirect, session, send_file, url_for
import pandas as pd
import os
from dotenv import load_dotenv
load_dotenv()
import json
import io
import traceback

# ========================
# PostgreSQL / Supabase
# ========================
import psycopg2
import psycopg2.extras

# ========================
# Google Drive OAuth
# ========================
from werkzeug.utils import secure_filename
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

SCOPES = ['https://www.googleapis.com/auth/drive']

# Required Render env variables:
# SUPABASE_DATABASE_URL  OR DATABASE_URL
# GOOGLE_CLIENT_SECRETS
# GOOGLE_REDIRECT_URI
# GOOGLE_TOKEN_JSON
# PARENT_FOLDER_ID

# ========================
# APP CONFIG
# ========================

app = Flask(
    __name__,
    static_folder='static',
    template_folder='templates'
)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "secret123")
# Maximum upload size = 200 MB
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

PARENT_FOLDER_ID = os.environ.get("PARENT_FOLDER_ID", "1SzrOrn93f3SDRBmWcYwhrLH3YUeQ-cuy")
TOKEN_FILE = "token.json"
drive_service = None

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
BENEFICIARY_ALLOWED_EXTENSIONS = {'xls', 'xlsx'}


# ========================
# PROJECT MASTER CONFIG
# ========================

PROJECT_MASTER_LIST = [
    {"slug": "education", "name": "Education"},
    {"slug": "women-empowerment", "name": "Women Empowerment"},
    {"slug": "agriculture", "name": "Agriculture"},
    {"slug": "environmental-climate", "name": "Environmental/Climate"},
    {"slug": "health-hygiene", "name": "Health & Hygiene"},
    {"slug": "hunger-malnutrition", "name": "Hunger & Malnutrition"},
]


PROJECT_PREFIX_MAP = {
    "education": "EDU",
    "women-empowerment": "WEP",
    "agriculture": "AGR",
    "environmental-climate": "ENV",
    "health-hygiene": "HHG",
    "hunger-malnutrition": "HMN",
}


# ========================
# DATABASE HELPERS
# ========================

def get_database_url():
    return os.environ.get("SUPABASE_DATABASE_URL") or os.environ.get("DATABASE_URL")


def get_db_connection():
    database_url = get_database_url()
    if not database_url:
        raise Exception("SUPABASE_DATABASE_URL or DATABASE_URL environment variable not found")
    return psycopg2.connect(database_url, sslmode="require")


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS school_records (
            id SERIAL PRIMARY KEY,
            udisc_number TEXT NOT NULL,
            school_code TEXT,
            school_name TEXT NOT NULL,
            location TEXT,
            year TEXT,
            girls INTEGER DEFAULT 0,
            boys INTEGER DEFAULT 0,
            total_students INTEGER DEFAULT 0,
            company_name TEXT,
            fy TEXT,
            phase TEXT,
            remarks TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS image_uploads (
            id SERIAL PRIMARY KEY,
            udisc_number TEXT NOT NULL,
            school_code TEXT,
            school_name TEXT NOT NULL,
            category TEXT NOT NULL,
            original_filename TEXT,
            drive_file_id TEXT,
            drive_file_name TEXT,
            drive_folder_id TEXT,
            drive_web_link TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS project_master (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            project_name TEXT NOT NULL,
            project_id TEXT,
            project_title TEXT,
            company_code TEXT,
            about_project TEXT,
            company_name TEXT,
            fy TEXT,
            project_cost NUMERIC DEFAULT 0,
            status TEXT DEFAULT 'Planning',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS project_info (
            id SERIAL PRIMARY KEY,
            project_id TEXT UNIQUE NOT NULL,
            project_title TEXT,
            company_code TEXT,
            company_name TEXT,
            project_cost NUMERIC DEFAULT 0,
            fy TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            code_verifier TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("ALTER TABLE school_records ADD COLUMN IF NOT EXISTS school_code TEXT")
    cur.execute("ALTER TABLE school_records ADD COLUMN IF NOT EXISTS school_name TEXT")
    cur.execute("ALTER TABLE school_records ADD COLUMN IF NOT EXISTS project_slug TEXT")
    cur.execute("ALTER TABLE school_records ADD COLUMN IF NOT EXISTS project_id TEXT")
    cur.execute("ALTER TABLE school_records ADD COLUMN IF NOT EXISTS company_code TEXT")
    cur.execute("ALTER TABLE school_records ADD COLUMN IF NOT EXISTS project_cost TEXT")
    cur.execute("ALTER TABLE image_uploads ADD COLUMN IF NOT EXISTS school_code TEXT")
    cur.execute("ALTER TABLE image_uploads ADD COLUMN IF NOT EXISTS school_name TEXT")
    cur.execute("ALTER TABLE image_uploads ADD COLUMN IF NOT EXISTS project_slug TEXT")
    cur.execute("ALTER TABLE project_master ADD COLUMN IF NOT EXISTS project_id TEXT")
    cur.execute("ALTER TABLE project_master ADD COLUMN IF NOT EXISTS project_title TEXT")
    cur.execute("ALTER TABLE project_master ADD COLUMN IF NOT EXISTS company_code TEXT")
    cur.execute("ALTER TABLE project_info ADD COLUMN IF NOT EXISTS project_title TEXT")

    for project in PROJECT_MASTER_LIST:
        cur.execute("""
            INSERT INTO project_master (slug, project_name)
            VALUES (%s, %s)
            ON CONFLICT (slug) DO NOTHING
        """, (project["slug"], project["name"]))

    conn.commit()
    cur.close()
    conn.close()


def save_school_to_db(data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO school_records (
            udisc_number, school_code, school_name, location, year, girls, boys, total_students,
            company_name, fy, phase, remarks, project_slug, project_id, company_code, project_cost
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        data["UDISC Number"], data["School Code"], data["School_Name"], data["Location"], data["Year"],
        data["Girls"], data["Boys"], data["Total Students"], data["Company Name"],
        data["FY"], data["Phase"], data["Remarks"], data.get("Project Slug", "education"),
        data.get("Project ID", ""), data.get("Company Code", ""), data.get("Project Cost", "")
    ))
    conn.commit()
    cur.close()
    conn.close()


def save_image_to_db(data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO image_uploads (
            udisc_number, school_code, school_name, category, original_filename,
            drive_file_id, drive_file_name, drive_folder_id, drive_web_link, project_slug
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        data["udisc_number"], data["school_code"], data["school_name"], data["category"],
        data["original_filename"], data["drive_file_id"], data["drive_file_name"],
        data["drive_folder_id"], data["drive_web_link"], data.get("project_slug", "education")
    ))
    conn.commit()
    cur.close()
    conn.close()




def get_project_master(slug):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, slug, project_name, project_id, project_title, company_code, about_project, company_name, fy, project_cost, status, created_at, updated_at
        FROM project_master
        WHERE slug = %s
    """, (slug,))
    project = cur.fetchone()
    cur.close()
    conn.close()
    return project


def get_all_project_master():
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, slug, project_name, project_id, project_title, company_code, about_project, company_name, fy, project_cost, status, created_at, updated_at
        FROM project_master
        ORDER BY id ASC
    """)
    projects = cur.fetchall()
    cur.close()
    conn.close()
    return projects


def update_project_master(slug, data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE project_master
        SET project_id = %s,
            project_title = %s,
            company_code = %s,
            about_project = %s,
            company_name = %s,
            fy = %s,
            project_cost = %s,
            status = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE slug = %s
    """, (
        data.get("project_id", ""),
        data.get("project_title", ""),
        data.get("company_code", ""),
        data.get("about_project", ""),
        data.get("company_name", ""),
        data.get("fy", ""),
        data.get("project_cost") or 0,
        data.get("status", "Planning"),
        slug
    ))
    conn.commit()
    cur.close()
    conn.close()


def get_project_stats(slug):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) AS total_records FROM school_records WHERE project_slug = %s", (slug,))
    record_count = cur.fetchone()["total_records"]
    cur.execute("SELECT COUNT(*) AS total_uploads FROM image_uploads WHERE project_slug = %s", (slug,))
    upload_count = cur.fetchone()["total_uploads"]
    cur.close()
    conn.close()
    return {"total_records": record_count, "total_uploads": upload_count}




def save_project_info(data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO project_info (project_id, project_title, company_code, company_name, project_cost, fy)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (project_id) DO UPDATE SET
            project_title = EXCLUDED.project_title,
            company_code = EXCLUDED.company_code,
            company_name = EXCLUDED.company_name,
            project_cost = EXCLUDED.project_cost,
            fy = EXCLUDED.fy,
            updated_at = CURRENT_TIMESTAMP
    """, (
        data.get("project_id", "").strip().upper(),
        data.get("project_title", "").strip(),
        data.get("company_code", "").strip().upper(),
        data.get("company_name", "").strip(),
        data.get("project_cost") or 0,
        data.get("fy", "").strip()
    ))
    conn.commit()
    cur.close()
    conn.close()


def get_project_info_by_id(project_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, project_id, project_title, company_code, company_name, project_cost, fy, created_at, updated_at
        FROM project_info
        WHERE UPPER(TRIM(project_id)) = UPPER(TRIM(%s))
        LIMIT 1
    """, ((project_id or "").strip(),))
    record = cur.fetchone()
    cur.close()
    conn.close()
    return record


def get_project_info_records():
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, project_id, project_title, company_code, company_name, project_cost, fy, created_at, updated_at
        FROM project_info
        ORDER BY id DESC
    """)
    records = cur.fetchall()
    cur.close()
    conn.close()
    return records


def get_project_ids_by_project_slug(project_slug):
    init_db()
    prefix = PROJECT_PREFIX_MAP.get(project_slug, "")
    if not prefix:
        return []

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT project_id
        FROM project_info
        WHERE UPPER(TRIM(project_id)) LIKE %s
        ORDER BY project_id ASC
    """, (prefix + "-%",))
    records = cur.fetchall()
    cur.close()
    conn.close()
    return records


@app.route('/get-project-ids/<project_slug>')
def get_project_ids_route(project_slug):
    if 'user' not in session:
        return {"items": [], "error": "Not logged in"}

    try:
        records = get_project_ids_by_project_slug(project_slug)
        return {"items": [row.get("project_id") for row in records]}
    except Exception as e:
        print("PROJECT ID LIST ERROR:")
        print(traceback.format_exc())
        return {"items": [], "error": str(e)}




def get_project_info_record_by_id(record_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, project_id, project_title, company_code, company_name, project_cost, fy, created_at, updated_at
        FROM project_info
        WHERE id = %s
    """, (record_id,))
    record = cur.fetchone()
    cur.close()
    conn.close()
    return record


def update_project_info_record(record_id, data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE project_info
        SET project_id = %s,
            project_title = %s,
            company_code = %s,
            company_name = %s,
            project_cost = %s,
            fy = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (
        data.get("project_id", "").strip().upper(),
        data.get("project_title", "").strip(),
        data.get("company_code", "").strip().upper(),
        data.get("company_name", "").strip(),
        data.get("project_cost") or 0,
        data.get("fy", "").strip(),
        record_id
    ))
    conn.commit()
    cur.close()
    conn.close()


def delete_project_info_record(record_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM project_info WHERE id = %s", (record_id,))
    conn.commit()
    cur.close()
    conn.close()

def school_code_exists(school_code, exclude_id=None):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()

    if exclude_id:
        cur.execute(
            "SELECT 1 FROM school_records WHERE school_code = %s AND id != %s LIMIT 1",
            (school_code, exclude_id)
        )
    else:
        cur.execute(
            "SELECT 1 FROM school_records WHERE school_code = %s LIMIT 1",
            (school_code,)
        )

    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists


def get_school_by_udisc(udisc_number):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, udisc_number, school_code, school_name
        FROM school_records
        WHERE udisc_number = %s
        ORDER BY id DESC
        LIMIT 1
    """, (udisc_number,))
    record = cur.fetchone()
    cur.close()
    conn.close()
    return record


def get_school_record_by_id(record_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, udisc_number, school_code, school_name, location, year, girls, boys,
               total_students, company_name, fy, phase, remarks, project_slug, created_at
        FROM school_records
        WHERE id = %s
    """, (record_id,))
    record = cur.fetchone()
    cur.close()
    conn.close()
    return record


def update_school_record(record_id, data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE school_records
        SET
            udisc_number = %s,
            school_code = %s,
            school_name = %s,
            location = %s,
            year = %s,
            girls = %s,
            boys = %s,
            total_students = %s,
            company_name = %s,
            fy = %s,
            phase = %s,
            remarks = %s
        WHERE id = %s
    """, (
        data["UDISC Number"],
        data["School Code"],
        data["School_Name"],
        data["Location"],
        data["Year"],
        data["Girls"],
        data["Boys"],
        data["Total Students"],
        data["Company Name"],
        data["FY"],
        data["Phase"],
        data["Remarks"],
        record_id
    ))
    conn.commit()
    cur.close()
    conn.close()


def delete_school_record(record_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM school_records WHERE id = %s", (record_id,))
    conn.commit()
    cur.close()
    conn.close()


def get_school_records(project_slug=None):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if project_slug:
        cur.execute("""
            SELECT id, udisc_number, school_code, school_name, location, year, girls, boys,
                   total_students, company_name, fy, phase, remarks, project_slug, created_at
            FROM school_records
            WHERE project_slug = %s
            ORDER BY id DESC
        """, (project_slug,))
    else:
        cur.execute("""
            SELECT id, udisc_number, school_code, school_name, location, year, girls, boys,
                   total_students, company_name, fy, phase, remarks, project_slug, created_at
            FROM school_records
            ORDER BY id DESC
        """)
    records = cur.fetchall()
    cur.close()
    conn.close()
    return records

def get_upload_records(project_slug=None):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if project_slug:
        cur.execute("""
            SELECT id, udisc_number, school_code, school_name, category, original_filename,
                   drive_file_id, drive_file_name, drive_folder_id, drive_web_link, project_slug, uploaded_at
            FROM image_uploads
            WHERE project_slug = %s
            ORDER BY id DESC
        """, (project_slug,))
    else:
        cur.execute("""
            SELECT id, udisc_number, school_code, school_name, category, original_filename,
                   drive_file_id, drive_file_name, drive_folder_id, drive_web_link, project_slug, uploaded_at
            FROM image_uploads
            ORDER BY id DESC
        """)
    records = cur.fetchall()
    cur.close()
    conn.close()
    return records



def get_upload_record_by_id(record_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, udisc_number, school_code, school_name, category, original_filename,
               drive_file_id, drive_file_name, drive_folder_id, drive_web_link, project_slug, uploaded_at
        FROM image_uploads
        WHERE id = %s
    """, (record_id,))
    record = cur.fetchone()
    cur.close()
    conn.close()
    return record


def update_upload_record(record_id, data):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE image_uploads
        SET udisc_number = %s,
            school_code = %s,
            category = %s,
            original_filename = %s
        WHERE id = %s
    """, (
        data.get("udisc_number", "").strip(),
        data.get("school_code", "").strip(),
        data.get("category", "").strip(),
        data.get("original_filename", "").strip(),
        record_id
    ))
    conn.commit()
    cur.close()
    conn.close()


def delete_upload_record(record_id):
    init_db()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM image_uploads WHERE id = %s", (record_id,))
    conn.commit()
    cur.close()
    conn.close()


# ========================
# OAUTH STATE / PKCE HELPERS
# ========================

def save_oauth_state(state, code_verifier):
    """Store Google OAuth PKCE code_verifier outside Flask session so Render redirects do not break authorization."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            code_verifier TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        INSERT INTO oauth_states (state, code_verifier)
        VALUES (%s, %s)
        ON CONFLICT (state) DO UPDATE SET code_verifier = EXCLUDED.code_verifier, created_at = CURRENT_TIMESTAMP
    """, (state, code_verifier))
    conn.commit()
    cur.close()
    conn.close()


def get_oauth_code_verifier(state):
    if not state:
        return None
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT code_verifier FROM oauth_states WHERE state = %s", (state,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def delete_oauth_state(state):
    if not state:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM oauth_states WHERE state = %s", (state,))
    conn.commit()
    cur.close()
    conn.close()

# ========================
# GOOGLE DRIVE OAUTH HELPERS
# ========================

def normalize_token_json(token_json):
    if not token_json:
        return None
    try:
        if isinstance(token_json, dict):
            return token_json
        token_json = str(token_json).strip()
        # Render sometimes stores copied values with surrounding quotes or spaces.
        if (token_json.startswith("'") and token_json.endswith("'")) or (token_json.startswith('"') and token_json.endswith('"')):
            try:
                token_json = json.loads(token_json)
            except Exception:
                token_json = token_json[1:-1]
        return json.loads(token_json)
    except Exception as e:
        print("❌ GOOGLE_TOKEN_JSON parse error:", str(e))
        return None


def ensure_google_token_table():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS google_oauth_tokens (
                id INTEGER PRIMARY KEY,
                token_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print("❌ GOOGLE TOKEN TABLE ERROR:", repr(e))
        return False


def save_token_to_db(token_data):
    try:
        if not ensure_google_token_table():
            return False
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO google_oauth_tokens (id, token_json, updated_at)
            VALUES (1, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
                token_json = EXCLUDED.token_json,
                updated_at = CURRENT_TIMESTAMP
        """, (json.dumps(token_data),))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print("❌ SAVE GOOGLE TOKEN TO DB ERROR:", repr(e))
        return False


def load_token_from_db():
    try:
        if not ensure_google_token_table():
            return None
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT token_json FROM google_oauth_tokens WHERE id = 1 LIMIT 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            return normalize_token_json(row[0])
    except Exception as e:
        print("❌ LOAD GOOGLE TOKEN FROM DB ERROR:", repr(e))
    return None

def delete_token_from_db():
    """Remove saved Google OAuth token from PostgreSQL when it becomes invalid."""
    try:
        if not ensure_google_token_table():
            return False
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM google_oauth_tokens WHERE id = 1")
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print("❌ DELETE GOOGLE TOKEN FROM DB ERROR:", repr(e))
        return False


def clear_saved_google_token():
    """Clear saved Google OAuth token from DB and local token file only."""
    delete_token_from_db()
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
    except Exception as e:
        print("⚠ Could not delete local token file:", repr(e))



def save_token(creds):
    token_data = json.loads(creds.to_json())
    # Preserve the existing refresh_token if Google does not return it on a later consent/login.
    # Without this, uploads can later fail with invalid_grant/expired credentials.
    if not token_data.get("refresh_token"):
        old_token_data = load_token_from_db() or normalize_token_json(os.environ.get("GOOGLE_TOKEN_JSON"))
        if old_token_data and old_token_data.get("refresh_token"):
            token_data["refresh_token"] = old_token_data.get("refresh_token")
    # Keep existing local token file behavior for local development.
    try:
        with open(TOKEN_FILE, "w") as token_file:
            json.dump(token_data, token_file)
    except Exception as e:
        print("⚠ Could not save local token file:", repr(e))
    # New Render-safe behavior: save token in Supabase/PostgreSQL so it survives redeploys.
    save_token_to_db(token_data)
    return token_data


def load_token():
    """
    Load Google OAuth token.

    Supabase/PostgreSQL token is preferred first because /authorize saves the latest
    working token there. GOOGLE_TOKEN_JSON in Render can become old/stale and should
    only be used as a backup.
    """

    # Priority 1: Supabase/PostgreSQL token saved after /authorize.
    token_data = load_token_from_db()
    if token_data:
        return token_data

    # Priority 2: Render environment variable, useful as backup only.
    token_data = normalize_token_json(os.environ.get("GOOGLE_TOKEN_JSON"))
    if token_data:
        return token_data

    # Priority 3: Local token file, useful while testing on local machine.
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as token_file:
                return json.load(token_file)
        except Exception as e:
            print("❌ LOCAL TOKEN FILE READ ERROR:", repr(e))

    return None


def get_drive_service():
    """
    Build a fresh Google Drive service from the saved OAuth token whenever needed.
    This prevents upload failures after Render restart when global drive_service is None.
    """
    try:
        token_data = load_token()

        if not token_data:
            print("❌ Google OAuth token not found. Open /authorize first.")
            return None

        creds = Credentials.from_authorized_user_info(token_data, SCOPES)

        if creds and creds.expired and creds.refresh_token:
            print("🔄 Google OAuth token expired. Refreshing token...")
            creds.refresh(Request())
            save_token(creds)

        if not creds or not creds.valid:
            print("❌ Google OAuth credentials are invalid. Open /authorize again.")
            return None

        service = build('drive', 'v3', credentials=creds, cache_discovery=False)

        # Confirm the token can actually access the configured parent folder.
        service.files().get(
            fileId=PARENT_FOLDER_ID,
            fields='id,name',
            supportsAllDrives=True
        ).execute()

        print("✅ Google Drive OAuth Connected Successfully")
        return service

    except Exception as e:
        print("❌ GOOGLE DRIVE OAUTH CONNECTION ERROR:", repr(e))
        if "invalid_grant" in str(e):
            clear_saved_google_token()
            print("⚠ Saved Google OAuth token was invalid and has been cleared. Open /connect-central-drive again.")
        return None


def escape_drive_query(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS



def build_school_drive_folder_name(udisc_number, school_code):
    safe_udisc = secure_filename(str(udisc_number).strip())
    safe_school_code = secure_filename(str(school_code).strip())
    return f"{safe_udisc}_{safe_school_code}"


def build_education_drive_folder_name(project_id, udisc_number, school_code):
    safe_project_id = secure_filename(str(project_id).strip())
    safe_udisc = secure_filename(str(udisc_number).strip())
    safe_school_code = secure_filename(str(school_code).strip())
    return f"{safe_project_id}_{safe_udisc}_{safe_school_code}"


def build_other_project_drive_folder_name(project_id, company_code):
    safe_project_id = secure_filename(str(project_id).strip())
    safe_company_code = secure_filename(str(company_code).strip())
    return f"{safe_project_id}_{safe_company_code}"


def create_folder(name, parent_id):
    global drive_service
    # Always rebuild from the saved central OAuth token so expired/stale in-memory services are not reused.
    drive_service = get_drive_service()
    if not drive_service:
        print("❌ Drive service not initialized")
        return None
    try:
        print(f"📁 Creating folder: {name}")
        safe_name = escape_drive_query(name)
        query = (
            f"name='{safe_name}' and "
            f"'{parent_id}' in parents and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"trashed=false"
        )
        response = drive_service.files().list(
            q=query,
            fields='files(id, name)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = response.get("files", [])
        if files:
            print(f"✅ Folder already exists: {name}")
            return files[0]["id"]
        file_metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = drive_service.files().create(
            body=file_metadata,
            fields='id',
            supportsAllDrives=True
        ).execute()
        folder_id = folder.get("id")
        print(f"✅ Folder created: {folder_id}")
        return folder_id
    except Exception as e:
        print("❌ FOLDER CREATION ERROR:", repr(e))
        drive_service = None
        return None


def upload_file(file, folder_id):
    global drive_service
    # Always rebuild from the saved central OAuth token so uploads use the central Drive account.
    drive_service = get_drive_service()
    if not drive_service or not folder_id:
        raise Exception("Central Google Drive is not connected. Open /connect-central-drive once and authorize ouproject2022@gmail.com.")
    try:
        filename = secure_filename(file.filename)
        print("📤 Uploading:", filename)
        file.seek(0)
        file_bytes = io.BytesIO(file.read())
        file_bytes.seek(0)
        if file_bytes.getbuffer().nbytes == 0:
            raise Exception(f"File is empty: {filename}")
        media = MediaIoBaseUpload(
            file_bytes,
            mimetype=file.content_type or "application/octet-stream",
            resumable=True
        )
        file_metadata = {'name': filename, 'parents': [folder_id]}
        request_upload = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,name,parents,webViewLink',
            supportsAllDrives=True
        )
        uploaded_file = None
        while uploaded_file is None:
            status, uploaded_file = request_upload.next_chunk()
        print(f"✅ Uploaded SUCCESS: {uploaded_file.get('name')} ID: {uploaded_file.get('id')}")
        return uploaded_file
    except HttpError as e:
        error_content = e.content.decode("utf-8") if hasattr(e, "content") else str(e)
        print("❌ FILE UPLOAD HTTP ERROR:", error_content)
        raise Exception(f"Google Drive upload failed for {file.filename}: {error_content}")
    except Exception as e:
        print("❌ FILE UPLOAD ERROR:", repr(e))
        drive_service = None
        raise e


def get_drive_folder_link_by_name(folder_name, parent_id):
    global drive_service

    if not drive_service:
        drive_service = get_drive_service()

    if not drive_service:
        return None

    try:
        safe_name = escape_drive_query(folder_name)

        query = (
            f"name='{safe_name}' and "
            f"'{parent_id}' in parents and "
            f"mimeType='application/vnd.google-apps.folder' and "
            f"trashed=false"
        )

        response = drive_service.files().list(
            q=query,
            fields='files(id, name, webViewLink)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        files = response.get("files", [])

        if not files:
            return None

        folder = files[0]

        return folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder.get('id')}"

    except Exception as e:
        print("❌ DRIVE FOLDER LINK LOOKUP ERROR:", repr(e))
        return None


def add_drive_folder_links_to_records(records):
    updated_records = []

    for record in records:
        record_dict = dict(record)

        udisc_number = record_dict.get("udisc_number") or ""
        school_code = record_dict.get("school_code") or ""
        project_slug = record_dict.get("project_slug") or "education"

        record_dict["drive_folder_link"] = None
        if udisc_number and school_code:
            if project_slug == "education":
                project = get_project_master("education")
                project_id = (project or {}).get("project_id") or ""
                if project_id:
                    folder_name = build_education_drive_folder_name(project_id, udisc_number, school_code)
                    record_dict["drive_folder_link"] = get_drive_folder_link_by_name(folder_name, PARENT_FOLDER_ID)
                if not record_dict["drive_folder_link"]:
                    old_folder_name = build_school_drive_folder_name(udisc_number, school_code)
                    record_dict["drive_folder_link"] = get_drive_folder_link_by_name(old_folder_name, PARENT_FOLDER_ID)
            else:
                folder_name = build_other_project_drive_folder_name(udisc_number, school_code)
                record_dict["drive_folder_link"] = get_drive_folder_link_by_name(folder_name, PARENT_FOLDER_ID)

        updated_records.append(record_dict)

    return updated_records


# ========================
# INITIALIZE DRIVE SERVICE
# ========================

# Do not initialize Google Drive during app startup.
# Render/Gunicorn must bind the web port quickly; Drive OAuth/DB checks are initialized lazily when needed.
drive_service = None

# ========================
# GOOGLE OAUTH ROUTES
# ========================

@app.route('/authorize')
def authorize():
    try:
        client_secrets_json = os.environ.get("GOOGLE_CLIENT_SECRETS")
        if not client_secrets_json:
            return "❌ GOOGLE_CLIENT_SECRETS environment variable not found"

        client_config = json.loads(client_secrets_json)
        redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", url_for('oauth2callback', _external=True))

        # Robust PKCE OAuth flow:
        # Google may require a code_verifier. We store it in Supabase by state instead
        # of relying only on Flask session, because Render/browser redirects can lose session data.
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=True
        )

        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )

        session['state'] = state
        if getattr(flow, "code_verifier", None):
            save_oauth_state(state, flow.code_verifier)

        return redirect(authorization_url)

    except Exception as e:
        return f"❌ OAuth Authorization Error: {str(e)}"


@app.route('/oauth2callback')
def oauth2callback():
    global drive_service
    try:
        client_secrets_json = os.environ.get("GOOGLE_CLIENT_SECRETS")
        if not client_secrets_json:
            return "❌ GOOGLE_CLIENT_SECRETS environment variable not found"

        client_config = json.loads(client_secrets_json)
        redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", url_for('oauth2callback', _external=True))
        state = request.args.get('state') or session.get('state')
        code_verifier = get_oauth_code_verifier(state)

        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            state=state,
            redirect_uri=redirect_uri
        )
        if code_verifier:
            flow.code_verifier = code_verifier

        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        token_data = save_token(creds)
        delete_oauth_state(state)
        drive_service = get_drive_service()

        return render_template('oauth_callback.html', token_json=json.dumps(token_data))

    except Exception as e:
        return f"❌ OAuth Callback Error: {str(e)}"



@app.route('/oauth-status')
def oauth_status():
    global drive_service
    drive_service = get_drive_service()
    connected = drive_service is not None
    return render_template('oauth_status.html', connected=connected)

# ========================
# LOGIN / MENU ROUTES
# ========================

@app.route('/', methods=['GET', 'POST'])
def login():
    error = ""
    if request.method == 'POST':
        if request.form['username'] == 'admin' and request.form['password'] == 'admin123':
            session['user'] = 'admin'
            return redirect('/menu')
        else:
            error = "Invalid login"
    return render_template('login.html', error=error)


@app.route('/menu')
def menu():
    if 'user' not in session:
        return redirect('/')
    return render_template('menu.html', projects=PROJECT_MASTER_LIST)


@app.route('/projects')
def projects():
    if 'user' not in session:
        return redirect('/')
    return render_template('projects.html', projects=PROJECT_MASTER_LIST)


@app.route('/project/<slug>', methods=['GET', 'POST'])
def project_master(slug):
    if 'user' not in session:
        return redirect('/')

    allowed_slugs = [project["slug"] for project in PROJECT_MASTER_LIST]
    if slug not in allowed_slugs:
        return "Project not found"

    success = False
    try:
        init_db()
        if request.method == 'POST':
            update_project_master(slug, {
                "project_id": request.form.get("project_id", "").strip().upper(),
                "project_title": request.form.get("project_title", ""),
                "company_code": request.form.get("company_code", "").strip().upper(),
                "about_project": request.form.get("about_project", ""),
                "company_name": request.form.get("company_name", ""),
                "fy": request.form.get("fy", ""),
                "project_cost": request.form.get("project_cost", 0),
                "status": request.form.get("status", "Planning")
            })
            success = True

        project = get_project_master(slug)
        stats = get_project_stats(slug)
        project_ids = get_project_ids_by_project_slug(slug)
        return render_template('project_master.html', project=project, stats=stats, success=success, project_ids=project_ids)

    except Exception as e:
        error_text = traceback.format_exc()
        print("PROJECT MASTER ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/project-data-entry', methods=['GET', 'POST'])
def project_data_entry():
    if 'user' not in session:
        return redirect('/')
    success = False
    try:
        if request.method == 'POST':
            project_id = request.form.get('project_id', '').strip()
            if not project_id:
                return "Project ID is required"
            save_project_info({
                "project_id": project_id,
                "project_title": request.form.get('project_title', ''),
                "company_code": request.form.get('company_code', ''),
                "company_name": request.form.get('company_name', ''),
                "project_cost": request.form.get('project_cost', 0),
                "fy": request.form.get('fy', '')
            })
            success = True
        records = get_project_info_records()
        return render_template('project_data_entry.html', records=records, success=success)
    except Exception as e:
        error_text = traceback.format_exc()
        print("PROJECT DATA ENTRY ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/edit-project-info/<int:record_id>', methods=['GET', 'POST'])
def edit_project_info(record_id):
    if 'user' not in session:
        return redirect('/')
    try:
        record = get_project_info_record_by_id(record_id)
        if not record:
            return "Project data entry not found"
        if request.method == 'POST':
            project_id = request.form.get('project_id', '').strip()
            if not project_id:
                return "Project ID is required"
            update_project_info_record(record_id, {
                "project_id": project_id,
                "project_title": request.form.get('project_title', ''),
                "company_code": request.form.get('company_code', ''),
                "company_name": request.form.get('company_name', ''),
                "project_cost": request.form.get('project_cost', 0),
                "fy": request.form.get('fy', '')
            })
            return redirect('/project-data-entry')
        return render_template('edit_project_info.html', record=record)
    except Exception as e:
        error_text = traceback.format_exc()
        print("EDIT PROJECT INFO ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/delete-project-info/<int:record_id>', methods=['POST'])
def delete_project_info(record_id):
    if 'user' not in session:
        return redirect('/')
    try:
        delete_project_info_record(record_id)
        return redirect('/project-data-entry')
    except Exception as e:
        error_text = traceback.format_exc()
        print("DELETE PROJECT INFO ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/get-project-info/<path:project_id>')
def get_project_info_route(project_id):
    if 'user' not in session:
        return {"found": False, "error": "Not logged in"}
    try:
        record = get_project_info_by_id((project_id or "").strip())
        if not record:
            return {"found": False}
        return {
            "found": True,
            "project_id": record.get("project_id") or "",
            "project_title": record.get("project_title") or "",
            "company_code": record.get("company_code") or "",
            "company_name": record.get("company_name") or "",
            "project_cost": str(record.get("project_cost") or ""),
            "fy": record.get("fy") or ""
        }
    except Exception as e:
        print("PROJECT INFO LOOKUP ERROR:")
        print(traceback.format_exc())
        return {"found": False, "error": str(e)}


@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    return redirect('/menu')

# ========================
# SCHOOL DATA ENTRY
# ========================

@app.route('/school-entry', methods=['GET', 'POST'])
def school_entry():
    if 'user' not in session:
        return redirect('/')
    success = False
    if request.method == 'POST':
        try:
            boys = int(request.form.get('boys') or 0)
            girls = int(request.form.get('girls') or 0)
            school_code = request.form.get('school_code', '').strip()
            school_name = request.form.get('school_name', '').strip()
            udisc_number = request.form.get('udisc', '').strip()
            if not school_code:
                return "School code is required"

            if school_code_exists(school_code):
                return "<script>alert('Duplicate School Code detected. Please enter a unique School Code.');window.history.back();</script>"

            if not school_name:
                return "School name is required"
            if not udisc_number:
                return "UDISC number is required"
            data = {
                "UDISC Number": udisc_number,
                "School Code": school_code,
                "School_Name": school_name,
                "Location": request.form.get('location', ''),
                "Year": request.form.get('year', ''),
                "Girls": girls,
                "Boys": boys,
                "Total Students": boys + girls,
                "Company Name": request.form.get('company', ''),
                "Company Code": request.form.get('company_code', ''),
                "Project ID": request.form.get('project_id', ''),
                "Project Cost": request.form.get('project_cost', ''),
                "FY": request.form.get('fy', ''),
                "Phase": request.form.get('phase', ''),
                "Remarks": request.form.get('remarks', ''),
                "Project Slug": request.form.get('project_slug', 'education')
            }
            save_school_to_db(data)
            success = True
        except Exception as e:
            error_text = traceback.format_exc()
            print("SCHOOL ENTRY ERROR:")
            print(error_text)
            return f"<pre>Error occurred:\n{error_text}</pre>"
    selected_project = request.args.get('project', 'education')
    project_master_data = get_project_master('education')
    project_ids = get_project_ids_by_project_slug('education')
    return render_template('school_entry.html', success=success, projects=PROJECT_MASTER_LIST, selected_project=selected_project, project_master=project_master_data, project_ids=project_ids)


@app.route('/get-school-by-udisc/<udisc_number>')
def get_school_by_udisc_route(udisc_number):
    if 'user' not in session:
        return {"found": False, "error": "Not logged in"}

    try:
        record = get_school_by_udisc(udisc_number)

        if not record:
            return {"found": False}

        return {
            "found": True,
            "school_code": record.get("school_code") or "",
            "school_name": record.get("school_name") or ""
        }

    except Exception as e:
        print("UDISC LOOKUP ERROR:")
        print(traceback.format_exc())
        return {"found": False, "error": str(e)}



@app.route('/check-school-code/<path:school_code>')
def check_school_code(school_code):
    if 'user' not in session:
        return {"exists": False}

    try:
        school_code = school_code.strip()

        if not school_code:
            return {"exists": False}

        exists = school_code_exists(school_code)
        return {"exists": exists}

    except Exception as e:
        print("SCHOOL CODE CHECK ERROR:")
        print(traceback.format_exc())
        return {"exists": False, "error": str(e)}


# ========================
# IMAGE UPLOAD
# ========================

@app.route('/image-upload', methods=['GET', 'POST'])
def image_upload():
    if 'user' not in session:
        return redirect('/')
    success = False
    selected_project = request.args.get('project', request.form.get('project_slug', 'education'))
    project = get_project_master(selected_project)
    if not project:
        return "Project not found"
    if request.method == 'POST':
        try:
            upload_count = 0
            if selected_project == 'education':
                project_id = (request.form.get('project_id') or project.get('project_id') or '').strip()
                udisc_number = request.form.get('udisc', '').strip()

                if not project_id:
                    return "Project ID is required in Education project master"
                if not udisc_number:
                    return "UDISC number is required"

                school_record = get_school_by_udisc(udisc_number)

                if not school_record:
                    return "No school record found for this UDISC Number. Please enter school data first."

                school_code = (school_record.get("school_code") or "").strip()
                school_name = (school_record.get("school_name") or "").strip()

                if not school_code:
                    return "School code not found in database for this UDISC Number"

                if not school_name:
                    return "School name not found in database for this UDISC Number"

                main_folder_name = build_education_drive_folder_name(project_id, udisc_number, school_code)
                school_folder_id = create_folder(main_folder_name, PARENT_FOLDER_ID)
                if not school_folder_id:
                    return "❌ Failed to create School folder in Google Drive. Please check OAuth Status and verify PARENT_FOLDER_ID access."
                folders = {
                    "smart_class": create_folder("Smart_Class", school_folder_id),
                    "ro": create_folder("RO", school_folder_id),
                    "sanitary": create_folder("Sanitary", school_folder_id),
                    "toilet": create_folder("Toilet", school_folder_id),
                    "other_photos": create_folder("Other_Photos", school_folder_id)
                }

                for field, folder_id in folders.items():

                    if not folder_id:
                        print(f"⚠ Skipping {field} folder")
                        continue

                    selected_files = request.files.getlist(field)
                    if len(selected_files) > 30:
                       return f"Maximum 30 images allowed in {field.replace('_', ' ').title()} upload."

                    for file in selected_files:

                        if file and file.filename and allowed_file(file.filename):

                            uploaded_file = upload_file(file, folder_id)

                            upload_count += 1

                            save_image_to_db({
                                "udisc_number": udisc_number,
                                "school_code": school_code,
                                "school_name": school_name,
                                "category": field,
                                "original_filename": file.filename,
                                "drive_file_id": uploaded_file.get("id"),
                                "drive_file_name": uploaded_file.get("name"),
                                "drive_folder_id": folder_id,
                                "drive_web_link": uploaded_file.get("webViewLink"),
                                "project_slug": selected_project
                            })
            else:
                project_id = (request.form.get('project_id') or project.get('project_id') or '').strip()
                company_code = (request.form.get('company_code') or project.get('company_code') or '').strip()
                if not project_id:
                    return "Project ID is required in project master"
                if not company_code:
                    return "Company Code is required in project master"

                main_folder_name = build_other_project_drive_folder_name(project_id, company_code)
                project_folder_id = create_folder(main_folder_name, PARENT_FOLDER_ID)
                if not project_folder_id:
                    return "❌ Failed to create Project folder in Google Drive. Please check OAuth Status and verify PARENT_FOLDER_ID access."

                selected_files = request.files.getlist('project_files')
                if len(selected_files) > 30:
                   return "Maximum 30 files allowed per upload."
                for file in selected_files:
                    if file and file.filename:
                        uploaded_file = upload_file(file, project_folder_id)
                        upload_count += 1
                        save_image_to_db({
                            "udisc_number": project_id,
                            "school_code": company_code,
                            "school_name": project.get('project_name') or selected_project,
                            "category": "project_files",
                            "original_filename": file.filename,
                            "drive_file_id": uploaded_file.get("id"),
                            "drive_file_name": uploaded_file.get("name"),
                            "drive_folder_id": project_folder_id,
                            "drive_web_link": uploaded_file.get("webViewLink"),
                            "project_slug": selected_project
                        })

            if upload_count == 0:
                return "No valid files selected."
            success = True
        except Exception as e:
            error_text = traceback.format_exc()
            print("IMAGE UPLOAD ERROR:")
            print(error_text)
            return f"<pre>Error occurred:\n{error_text}</pre>"
    project_ids = get_project_ids_by_project_slug(selected_project)
    return render_template('image_upload.html', success=success, upload_count=locals().get("upload_count", 0), project=project, project_ids=project_ids)


# ========================
# EDIT / DELETE RECORD
# ========================

@app.route('/edit-record/<int:record_id>', methods=['GET', 'POST'])
def edit_record(record_id):
    if 'user' not in session:
        return redirect('/')

    try:
        record = get_school_record_by_id(record_id)

        if not record:
            return "Record not found"

        if request.method == 'POST':
            boys = int(request.form.get('boys') or 0)
            girls = int(request.form.get('girls') or 0)

            school_code = request.form.get('school_code', '').strip()
            school_name = request.form.get('school_name', '').strip()
            udisc_number = request.form.get('udisc', '').strip()

            if not school_code:
                return "School code is required"

            if school_code_exists(school_code, record_id):
                return "<script>alert('Duplicate School Code detected. Please enter a unique School Code.');window.history.back();</script>"

            if not school_name:
                return "School name is required"

            if not udisc_number:
                return "UDISC number is required"

            data = {
                "UDISC Number": udisc_number,
                "School Code": school_code,
                "School_Name": school_name,
                "Location": request.form.get('location', ''),
                "Year": request.form.get('year', ''),
                "Girls": girls,
                "Boys": boys,
                "Total Students": boys + girls,
                "Company Name": request.form.get('company', ''),
                "Company Code": request.form.get('company_code', ''),
                "Project ID": request.form.get('project_id', ''),
                "Project Cost": request.form.get('project_cost', ''),
                "FY": request.form.get('fy', ''),
                "Phase": request.form.get('phase', ''),
                "Remarks": request.form.get('remarks', '')
            }

            update_school_record(record_id, data)

            return redirect('/records')

        return render_template('edit_record.html', record=record)

    except Exception as e:
        error_text = traceback.format_exc()
        print("EDIT RECORD ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/delete-record/<int:record_id>', methods=['POST'])
def delete_record(record_id):
    if 'user' not in session:
        return redirect('/')

    try:
        delete_school_record(record_id)
        return redirect('/records')

    except Exception as e:
        error_text = traceback.format_exc()
        print("DELETE RECORD ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


# ========================
# RECORDS / EXPORT
# ========================

@app.route('/records')
def records():
    if 'user' not in session:
        return redirect('/')
    try:
        selected_project = request.args.get('project')
        school_records = get_school_records(selected_project)
        school_records = add_drive_folder_links_to_records(school_records)
        return render_template('records.html', records=school_records)
    except Exception as e:
        error_text = traceback.format_exc()
        print("RECORDS ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/upload-records')
def upload_records():
    if 'user' not in session:
        return redirect('/')
    try:
        selected_project = request.args.get('project')
        uploads = get_upload_records(selected_project)
        project_name = selected_project or "All Projects"
        project = get_project_master(selected_project) if selected_project else None
        if project:
            project_name = project.get('project_name') or project_name
        return render_template('upload_records.html', records=uploads, project_name=project_name)
    except Exception as e:
        error_text = traceback.format_exc()
        print("UPLOAD RECORDS ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/edit-upload-record/<int:record_id>', methods=['GET', 'POST'])
def edit_upload_record(record_id):
    if 'user' not in session:
        return redirect('/')
    try:
        record = get_upload_record_by_id(record_id)
        if not record:
            return "Upload record not found"
        if request.method == 'POST':
            update_upload_record(record_id, {
                "udisc_number": request.form.get('udisc_number', ''),
                "school_code": request.form.get('school_code', ''),
                "category": request.form.get('category', ''),
                "original_filename": request.form.get('original_filename', '')
            })
            return redirect('/upload-records?project=' + (record.get('project_slug') or ''))
        return render_template('edit_upload_record.html', record=record)
    except Exception as e:
        error_text = traceback.format_exc()
        print("EDIT UPLOAD RECORD ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/delete-upload-record/<int:record_id>', methods=['POST'])
def delete_upload_record_route(record_id):
    if 'user' not in session:
        return redirect('/')
    try:
        record = get_upload_record_by_id(record_id)
        project_slug = (record or {}).get('project_slug') or ''
        delete_upload_record(record_id)
        return redirect('/upload-records?project=' + project_slug)
    except Exception as e:
        error_text = traceback.format_exc()
        print("DELETE UPLOAD RECORD ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


@app.route('/export')
def export():
    if 'user' not in session:
        return redirect('/')
    try:
        records = get_school_records()
        records = add_drive_folder_links_to_records(records)
        df = pd.DataFrame(records)

        if not df.empty:
            df["drive_folder_link"] = df["drive_folder_link"].fillna("N/A")
            df.insert(0, "ID", range(1, len(df) + 1))

            rename_map = {
                "project_slug": "Project",
                "udisc_number": "UDISC",
                "school_code": "School Code",
                "school_name": "School_Name",
                "location": "Location",
                "year": "Year",
                "girls": "Girls",
                "boys": "Boys",
                "total_students": "Total",
                "company_name": "Company",
                "fy": "FY",
                "phase": "Phase",
                "remarks": "Remarks",
                "created_at": "Created",
                "drive_folder_link": "Google Drive Folder"
            }

            if "id" in df.columns:
                df = df.drop(columns=["id"])

            df = df.rename(columns=rename_map)

            ordered_columns = [
                "ID",
                "Project",
                "UDISC",
                "School Code",
                "School_Name",
                "Location",
                "Year",
                "Girls",
                "Boys",
                "Total",
                "Company",
                "FY",
                "Phase",
                "Remarks",
                "Created",
                "Google Drive Folder"
            ]

            df = df[[col for col in ordered_columns if col in df.columns]]

        export_file = "school_data_export.xlsx"
        df.to_excel(export_file, index=False, engine='openpyxl')
        return send_file(export_file, as_attachment=True)
    except Exception as e:
        error_text = traceback.format_exc()
        print("EXPORT ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


# ========================
# BENEFICIARY ACCOUNT HELPERS
# ========================

def allowed_beneficiary_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in BENEFICIARY_ALLOWED_EXTENSIONS


def beneficiary_file_sort_key(file_info):
    return file_info.get("createdTime") or file_info.get("modifiedTime") or ""


def list_beneficiary_excel_files():
    global drive_service
    if not drive_service:
        drive_service = get_drive_service()
    if not drive_service:
        return []
    try:
        query = (
            f"'{PARENT_FOLDER_ID}' in parents and "
            f"trashed=false and "
            f"(name contains 'Beneficiary' or name contains 'beneficiary')"
        )
        response = drive_service.files().list(
            q=query,
            fields="files(id, name, webViewLink, webContentLink, createdTime, modifiedTime, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="createdTime desc"
        ).execute()
        files = response.get("files", [])
        excel_files = [
            item for item in files
            if str(item.get("name", "")).lower().endswith((".xlsx", ".xls"))
        ]
        excel_files.sort(key=beneficiary_file_sort_key, reverse=True)
        return excel_files
    except Exception as e:
        print("❌ BENEFICIARY FILE LIST ERROR:", repr(e))
        return []


def get_latest_beneficiary_excel_file():
    files = list_beneficiary_excel_files()
    return files[0] if files else None


def download_drive_file_bytes(file_id):
    global drive_service
    if not drive_service:
        drive_service = get_drive_service()
    if not drive_service:
        raise Exception("Drive service not initialized. Open /authorize first.")

    request_download = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    file_bytes = io.BytesIO()
    downloader = MediaIoBaseDownload(file_bytes, request_download)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    file_bytes.seek(0)
    return file_bytes


def value_counts_as_list(series):
    counts = series.fillna("Blank").astype(str).str.strip().replace("", "Blank").value_counts()
    return [{"name": str(name), "count": int(count)} for name, count in counts.items()]


def calculate_percent(count, total):
    try:
        if not total:
            return 0
        return round((int(count) / int(total)) * 100, 1)
    except Exception:
        return 0


def analyze_beneficiary_excel_from_bytes(file_bytes):
    summary = {
        "sheet_sections": [],
        "errors": []
    }

    try:
        workbook = pd.read_excel(file_bytes, sheet_name=None)
    except Exception as e:
        summary["errors"].append(f"Unable to read Excel file: {str(e)}")
        return summary

    target_sheets = ["Uttrakhand", "Delhi_NCR"]
    required_columns = ["Project Name", "Beneficiary Group Name", "Gender", "District"]

    for sheet_name in target_sheets:
        if sheet_name not in workbook:
            summary["errors"].append(f"Sheet not found: {sheet_name}")
            continue

        df = workbook[sheet_name].copy()
        df = df.dropna(how="all")

        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            summary["errors"].append(
                f"Missing column(s) in sheet {sheet_name}: " + ", ".join(missing_columns)
            )
            continue

        df = df[required_columns].copy()

        df["Project Name"] = df["Project Name"].fillna("Blank").astype(str).str.strip().replace("", "Blank")
        df["Beneficiary Group Name"] = df["Beneficiary Group Name"].fillna("Blank").astype(str).str.strip().replace("", "Blank")
        df["Gender"] = df["Gender"].fillna("Blank").astype(str).str.strip().replace("", "Blank")
        df["District"] = df["District"].fillna("Blank").astype(str).str.strip().replace("", "Blank")

        total_beneficiaries = int(len(df))
        project_count = int(df["Project Name"].nunique())
        district_count = int(df["District"].nunique())
        group_count = int(df["Beneficiary Group Name"].nunique())

        gender_distribution = value_counts_as_list(df["Gender"])
        district_distribution = value_counts_as_list(df["District"])

        for item in gender_distribution:
            item["percent"] = calculate_percent(item["count"], total_beneficiaries)

        for item in district_distribution:
            item["percent"] = calculate_percent(item["count"], total_beneficiaries)

        project_cards = []

        for project_name, project_df in df.groupby("Project Name"):
            beneficiary_groups = value_counts_as_list(project_df["Beneficiary Group Name"])
            gender_counts = value_counts_as_list(project_df["Gender"])
            district_counts = value_counts_as_list(project_df["District"])

            card = {
                "project_name": project_name,
                "total_beneficiaries": int(len(project_df)),
                "beneficiary_group_count": int(project_df["Beneficiary Group Name"].nunique()),
                "district_count": int(project_df["District"].nunique()),
                "beneficiary_groups": beneficiary_groups,
                "gender_counts": gender_counts,
                "district_counts": district_counts
            }

            project_cards.append(card)

        project_cards = sorted(project_cards, key=lambda item: item["project_name"])

        summary["sheet_sections"].append({
            "sheet_name": sheet_name,
            "display_name": "Uttrakhand" if sheet_name == "Uttrakhand" else "Delhi NCR",
            "total_beneficiaries": total_beneficiaries,
            "project_count": project_count,
            "district_count": district_count,
            "group_count": group_count,
            "gender_distribution": gender_distribution,
            "district_distribution": district_distribution,
            "project_cards": project_cards
        })

    return summary


def get_beneficiary_summary_from_latest_drive_file():
    latest_file = get_latest_beneficiary_excel_file()
    if not latest_file:
        return None, {
            "sheet_sections": [],
            "errors": ["No Beneficiary Excel file found in parent Google Drive folder."]
        }

    try:
        file_bytes = download_drive_file_bytes(latest_file.get("id"))
        summary = analyze_beneficiary_excel_from_bytes(file_bytes)
        return latest_file, summary
    except Exception as e:
        return latest_file, {
            "sheet_sections": [],
            "errors": [str(e)]
        }


@app.route('/beneficiary-account', methods=['GET', 'POST'])
def beneficiary_account():
    if 'user' not in session:
        return redirect('/')

    success = False
    error = ""
    uploaded_file_info = None

    try:
        if request.method == 'POST':
            excel_file = request.files.get('beneficiary_excel')

            if not excel_file or not excel_file.filename:
                error = "Please select an Excel file to upload."
            elif not allowed_beneficiary_file(excel_file.filename):
                error = "Only .xls and .xlsx files are allowed."
            else:
                timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
                original_name = secure_filename(excel_file.filename)
                drive_name = f"Beneficiary_Account_{timestamp}_{original_name}"

                global drive_service
                if not drive_service:
                    drive_service = get_drive_service()

                if not drive_service:
                    error = "Google Drive OAuth is not connected or parent folder is not accessible. Please check OAuth Status and PARENT_FOLDER_ID access."
                else:
                    excel_file.filename = drive_name
                    uploaded = upload_file(excel_file, PARENT_FOLDER_ID)

                    uploaded_file_info = {
                        "id": uploaded.get("id"),
                        "name": uploaded.get("name"),
                        "webViewLink": uploaded.get("webViewLink")
                    }
                    success = True

        latest_file, summary = get_beneficiary_summary_from_latest_drive_file()
        files = list_beneficiary_excel_files()

        return render_template(
            'beneficiary_account.html',
            success=success,
            error=error,
            uploaded_file=uploaded_file_info,
            latest_file=latest_file,
            summary=summary,
            files=files
        )

    except Exception as e:
        error_text = traceback.format_exc()
        print("BENEFICIARY ACCOUNT ERROR:")
        print(error_text)
        return f"<pre>Error occurred:\n{error_text}</pre>"


# ========================
# TEST ROUTES
# ========================

@app.route('/test-db')
def test_db():
    try:
        init_db()
        return "✅ Supabase PostgreSQL connection OK"
    except Exception as e:
        error_text = traceback.format_exc()
        return f"<pre>❌ Supabase PostgreSQL Error:\n{error_text}</pre>"


@app.route('/test-drive')
def test_drive():
    global drive_service
    drive_service = get_drive_service()
    try:
        if not drive_service:
            return "❌ Drive service not initialized. Open /authorize first."
        parent = drive_service.files().get(
            fileId=PARENT_FOLDER_ID,
            fields='id,name',
            supportsAllDrives=True
        ).execute()
        folder_id = create_folder("TEST_FOLDER", PARENT_FOLDER_ID)
        return f"""
        ✅ Parent Folder Access OK <br><br>
        Parent Name: {parent['name']} <br><br>
        TEST_FOLDER ID: {folder_id}
        """
    except Exception as e:
        error_text = traceback.format_exc()
        print("TEST DRIVE ERROR:")
        print(error_text)
        return f"<pre>❌ Google Drive Error:\n{error_text}</pre>"

# ========================
# LOGOUT
# ========================

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')

# ========================
# RUN
# ========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
