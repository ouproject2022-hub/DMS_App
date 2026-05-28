import os
import io
import json
import traceback
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_file, abort, session
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_bcrypt import Bcrypt
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import pandas as pd

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///documents.db"
).replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "doc", "docx", "xls", "xlsx", "csv", "txt", "ppt", "pptx"}
SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_PARENT_FOLDER_ID = os.environ.get("GOOGLE_PARENT_FOLDER_ID", "").strip()
CENTRAL_DRIVE_EMAIL = os.environ.get("CENTRAL_DRIVE_EMAIL", "ouproject2022@gmail.com").strip().lower()

SECTIONS = {
    "ummid_ngo": {
        "label": "Ummid NGO",
        "roles": ["admin", "ngo"],
        "categories": [
            "Registration Documents", "School Documents", "Student Records",
            "Donation Records", "Invoices", "Reports", "Letters", "Others"
        ]
    },
    "mahalaxmi_trader": {
        "label": "Mahalaxmi Trader",
        "roles": ["admin", "trader"],
        "categories": [
            "GST Documents", "Bills", "Purchase Records", "Sales Records",
            "Bank Documents", "Tax Documents", "Letters", "Others"
        ]
    },
    "personal": {
        "label": "Personal Documents",
        "roles": ["admin", "personal"],
        "categories": [
            "Aadhaar", "PAN", "Bank", "Education", "Property",
            "Medical", "Insurance", "Others"
        ]
    }
}

ROLE_LABELS = {
    "admin": "Admin - All Access",
    "ngo": "Ummid NGO Staff",
    "trader": "Mahalaxmi Trader Staff",
    "personal": "Personal Documents User",
    "viewer": "Viewer"
}


db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "warning"


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(180), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default="viewer")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)


class DriveFolder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    section = db.Column(db.String(80), nullable=False, index=True)
    category = db.Column(db.String(120), nullable=False, index=True)
    year = db.Column(db.String(10), nullable=False, index=True)
    folder_name = db.Column(db.String(255), nullable=False)
    google_drive_folder_id = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    section = db.Column(db.String(80), nullable=False, index=True)
    document_category = db.Column(db.String(120), nullable=False, index=True)
    document_title = db.Column(db.String(255), nullable=False, index=True)
    related_name = db.Column(db.String(255), nullable=True, index=True)
    year = db.Column(db.String(10), nullable=False, index=True)
    file_name = db.Column(db.String(255), nullable=False)
    file_mime_type = db.Column(db.String(120), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    google_drive_file_id = db.Column(db.String(255), nullable=False)
    google_drive_file_link = db.Column(db.Text, nullable=False)
    google_drive_folder_id = db.Column(db.String(255), nullable=False)
    uploaded_by = db.Column(db.String(180), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    remarks = db.Column(db.Text, nullable=True)


class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def section_label(section_key):
    return SECTIONS.get(section_key, {}).get("label", section_key)


def user_allowed_sections(user):
    if user.role == "admin":
        return list(SECTIONS.keys())
    if user.role == "viewer":
        return list(SECTIONS.keys())
    return [key for key, meta in SECTIONS.items() if user.role in meta["roles"]]


def require_section_access(section_key, write=False):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if section_key not in SECTIONS:
                abort(404)
            if current_user.role == "viewer" and write:
                flash("Viewer role cannot upload or change documents.", "danger")
                return redirect(url_for("dashboard"))
            if section_key not in user_allowed_sections(current_user):
                abort(403)
            return func(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return func(*args, **kwargs)
    return wrapper


def get_oauth_client_config():
    oauth_json = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "").strip()
    if not oauth_json:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_JSON environment variable is missing.")

    try:
        config = json.loads(oauth_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_JSON is not valid JSON.") from exc

    if "web" not in config and "installed" not in config:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_JSON must be an OAuth Client JSON, not a Service Account JSON.")

    return config


def credentials_to_session_dict(creds):
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes
    }


def build_credentials_from_token_data(token_data):
    if not token_data:
        return None

    creds = Credentials(**token_data)

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())

    return creds


def get_google_credentials():
    token_data = session.get("google_token")
    creds = build_credentials_from_token_data(token_data)

    if creds and creds.expired is False:
        session["google_token"] = credentials_to_session_dict(creds)

    return creds


def get_drive_account_email(creds):
    try:
        service = build("drive", "v3", credentials=creds)
        about = service.about().get(fields="user(emailAddress)").execute()
        return (about.get("user", {}).get("emailAddress") or "").strip().lower()
    except Exception:
        return ""


def get_app_setting(key):
    setting = AppSetting.query.filter_by(key=key).first()
    return setting.value if setting else None


def set_app_setting(key, value):
    setting = AppSetting.query.filter_by(key=key).first()
    if not setting:
        setting = AppSetting(key=key, value=value)
        db.session.add(setting)
    else:
        setting.value = value
    db.session.commit()


def get_central_drive_token_data():
    token_json = os.environ.get("CENTRAL_GOOGLE_DRIVE_TOKEN_JSON", "").strip()
    if not token_json:
        token_json = os.environ.get("GOOGLE_DRIVE_TOKEN_JSON", "").strip()
    if token_json:
        return json.loads(token_json)

    stored_token = get_app_setting("central_google_drive_token")
    if stored_token:
        return json.loads(stored_token)

    return None


def save_central_drive_token(creds):
    set_app_setting("central_google_drive_token", json.dumps(credentials_to_session_dict(creds)))


def get_central_drive_credentials():
    token_data = get_central_drive_token_data()
    creds = build_credentials_from_token_data(token_data)

    if creds and creds.refresh_token and not os.environ.get("CENTRAL_GOOGLE_DRIVE_TOKEN_JSON", "").strip() and not os.environ.get("GOOGLE_DRIVE_TOKEN_JSON", "").strip():
        save_central_drive_token(creds)

    return creds


def get_service_account_drive_service():
    service_creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if not service_creds_json:
        return None

    try:
        creds_dict = json.loads(service_creds_json)
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        service_creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=SCOPES
        )
        return build("drive", "v3", credentials=service_creds)
    except Exception as exc:
        raise RuntimeError(f"Google Drive service account authorization failed: {exc}")


def get_drive_service():
    # Drive upload/delete is centralized through the OAuth token of CENTRAL_DRIVE_EMAIL.
    # Do not fall back to Service Account here because personal Gmail folders cannot use
    # Service Account storage quota. Do not fall back to each user's token because that
    # would again consume the uploader's personal Drive quota.
    central_creds = get_central_drive_credentials()
    if central_creds and central_creds.valid:
        return build("drive", "v3", credentials=central_creds)

    creds = get_google_credentials()
    if creds and creds.valid:
        connected_email = get_drive_account_email(creds)
        if connected_email == CENTRAL_DRIVE_EMAIL:
            save_central_drive_token(creds)
            return build("drive", "v3", credentials=creds)

    raise RuntimeError(f"Central Google Drive is not connected. Please login/connect once with {CENTRAL_DRIVE_EMAIL}.")

def drive_query_escape(value):
    return value.replace("'", "\\'")


def find_folder(service, name, parent_id):
    safe_name = drive_query_escape(name)
    query = (
        f"name='{safe_name}' and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"
    result = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def drive_file_exists_in_folder(service, filename, folder_id):
    safe_filename = drive_query_escape(filename)
    query = (
        f"name='{safe_filename}' and '{folder_id}' in parents "
        f"and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    )
    result = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    return bool(result.get("files", []))


def create_folder(service, name, parent_id=None):
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return folder["id"]


def find_or_create_folder(service, name, parent_id=None):
    folder_id = find_folder(service, name, parent_id)
    if folder_id:
        return folder_id
    return create_folder(service, name, parent_id)


def get_drive_folder_link(folder_id):
    return f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else "#"


def get_document_folder(service, section, category, company_name, year):
    if not GOOGLE_PARENT_FOLDER_ID:
        raise RuntimeError("GOOGLE_PARENT_FOLDER_ID environment variable is missing.")

    root_id = GOOGLE_PARENT_FOLDER_ID
    section_name = section_label(section)
    clean_company_name = secure_filename(company_name).replace("_", " ").strip() or "Company"
    company_year_folder = f"{clean_company_name} - {year}"

    section_id = find_or_create_folder(service, section_name, root_id)
    category_id = find_or_create_folder(service, category, section_id)
    company_year_id = find_or_create_folder(service, company_year_folder, category_id)
    folder_path = f"{section_name}/{category}/{company_year_folder}"

    existing = DriveFolder.query.filter_by(
        section=section,
        category=category,
        year=str(year),
        folder_name=folder_path
    ).first()
    if not existing:
        db.session.add(DriveFolder(
            section=section,
            category=category,
            year=str(year),
            folder_name=folder_path,
            google_drive_folder_id=company_year_id
        ))
        db.session.commit()
    elif existing.google_drive_folder_id != company_year_id:
        existing.google_drive_folder_id = company_year_id
        db.session.commit()

    return company_year_id


def upload_file_to_drive(file_storage, folder_id, service=None):
    if service is None:
        service = get_drive_service()
    filename = secure_filename(file_storage.filename)
    file_stream = io.BytesIO(file_storage.read())
    file_stream.seek(0)
    media = MediaIoBaseUpload(
        file_stream,
        mimetype=file_storage.mimetype or "application/octet-stream",
        resumable=True
    )
    metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    uploaded = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()
    return uploaded


def create_default_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "Admin@12345")
    admin_name = os.environ.get("ADMIN_NAME", "Admin")

    existing = User.query.filter_by(email=admin_email).first()
    if not existing:
        user = User(name=admin_name, email=admin_email, role="admin")
        user.set_password(admin_password)
        db.session.add(user)
        db.session.commit()


@app.context_processor
def inject_globals():
    return {
        "SECTIONS": SECTIONS,
        "ROLE_LABELS": ROLE_LABELS,
        "section_label": section_label,
        "user_allowed_sections": user_allowed_sections,
        "current_year": datetime.now().year,
        "related_name_label": "Company Name"
    }


@app.route("/authorize")
@login_required
def authorize():
    session["oauth_next_url"] = request.args.get("next") or url_for("dashboard")

    flow = Flow.from_client_config(
        get_oauth_client_config(),
        scopes=SCOPES,
        redirect_uri=url_for("oauth2callback", _external=True)
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    session["oauth_state"] = state
    return redirect(auth_url)




@app.route("/authorize-central-drive")
@app.route("/connect-central-drive")
@login_required
def authorize_central_drive():
    """Hidden route to connect the central Google Drive storage account.
    This does not change the existing app login flow. It only saves the
    Google Drive OAuth token for CENTRAL_DRIVE_EMAIL so all uploads/deletes
    can use the central Drive storage account.
    """
    session["oauth_next_url"] = request.args.get("next") or url_for("dashboard")
    session["central_drive_authorization"] = True

    flow = Flow.from_client_config(
        get_oauth_client_config(),
        scopes=SCOPES,
        redirect_uri=url_for("oauth2callback", _external=True)
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account",
        login_hint=CENTRAL_DRIVE_EMAIL
    )

    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/oauth2callback")
@login_required
def oauth2callback():
    state = session.get("oauth_state")
    if not state:
        flash("Google authorization session expired. Please connect Google Drive again.", "danger")
        return redirect(url_for("dashboard"))

    flow = Flow.from_client_config(
        get_oauth_client_config(),
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for("oauth2callback", _external=True)
    )

    flow.fetch_token(authorization_response=request.url)
    session["google_token"] = credentials_to_session_dict(flow.credentials)

    connected_email = get_drive_account_email(flow.credentials)
    is_central_authorization = session.pop("central_drive_authorization", False)

    if connected_email == CENTRAL_DRIVE_EMAIL:
        save_central_drive_token(flow.credentials)
        flash("Central Google Drive connected successfully for uploads and delete operations.", "success")
    elif is_central_authorization:
        flash(f"Central Google Drive was not connected. Please choose {CENTRAL_DRIVE_EMAIL} on the Google account selection screen.", "danger")
    else:
        flash("Google Drive connected successfully. Central storage is not changed because this is not the central Drive account.", "success")

    next_url = session.pop("oauth_next_url", url_for("dashboard"))
    return redirect(next_url)


@app.route("/disconnect-google-drive")
@login_required
def disconnect_google_drive():
    session.pop("google_token", None)
    session.pop("oauth_state", None)
    flash("Google Drive disconnected from this login session.", "info")
    return redirect(url_for("dashboard"))


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash("Login successful.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    cards = []
    for key in user_allowed_sections(current_user):
        total = Document.query.filter_by(section=key).count()
        cards.append({"key": key, "label": section_label(key), "total": total})
    recent_docs = Document.query.order_by(Document.uploaded_at.desc()).limit(8).all()
    return render_template("dashboard.html", cards=cards, recent_docs=recent_docs)


@app.route("/documents/<section>")
@login_required
def documents(section):
    if section not in SECTIONS:
        abort(404)
    if section not in user_allowed_sections(current_user):
        abort(403)

    query = Document.query.filter_by(section=section)
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    year = request.args.get("year", "").strip()

    if q:
        search = f"%{q}%"
        query = query.filter(
            db.or_(
                Document.document_title.ilike(search),
                Document.related_name.ilike(search),
                Document.file_name.ilike(search),
                Document.remarks.ilike(search)
            )
        )
    if category:
        query = query.filter_by(document_category=category)
    if year:
        query = query.filter_by(year=year)

    docs = query.order_by(Document.uploaded_at.desc()).all()

    # Show the Company Name + Year Google Drive folder when clicking "Open Drive".
    # Existing database column name is kept unchanged for compatibility with current templates.
    for doc in docs:
        if doc.google_drive_folder_id:
            doc.google_drive_file_link = get_drive_folder_link(doc.google_drive_folder_id)

    years = [row[0] for row in db.session.query(Document.year).filter_by(section=section).distinct().order_by(Document.year.desc()).all()]
    return render_template("documents.html", docs=docs, section=section, years=years, related_name_label="Company Name")


@app.route("/upload/<section>", methods=["GET", "POST"])
@login_required
def upload(section):
    if section not in SECTIONS:
        abort(404)
    if current_user.role == "viewer":
        flash("Viewer role cannot upload documents.", "danger")
        return redirect(url_for("documents", section=section))
    if section not in user_allowed_sections(current_user):
        abort(403)

    if request.method == "POST":
        category = request.form.get("category", "").strip()
        title = request.form.get("title", "").strip()
        company_name = request.form.get("company_name", "").strip() or request.form.get("related_name", "").strip()
        related_name = company_name
        year = request.form.get("year", str(datetime.now().year)).strip()
        remarks = request.form.get("remarks", "").strip()
        uploaded_files = request.files.getlist("files") or request.files.getlist("file")
        uploaded_files = [item for item in uploaded_files if item and item.filename]

        if not category or not title or not company_name or not year:
            flash("Category, title, company name and year are required.", "danger")
            return redirect(request.url)
        if category not in SECTIONS[section]["categories"]:
            flash("Invalid category selected.", "danger")
            return redirect(request.url)
        if not uploaded_files:
            flash("Please select at least one file.", "danger")
            return redirect(request.url)

        safe_names = []
        for upload_item in uploaded_files:
            if not allowed_file(upload_item.filename):
                flash(f"File type is not allowed: {upload_item.filename}", "danger")
                return redirect(request.url)
            safe_name = secure_filename(upload_item.filename)
            if safe_name in safe_names:
                flash(f"Duplicate file selected: {safe_name}", "warning")
                return redirect(request.url)
            safe_names.append(safe_name)
            duplicate = Document.query.filter_by(
                section=section,
                document_category=category,
                document_title=title,
                year=year,
                file_name=safe_name
            ).first()
            if duplicate:
                flash(f"Duplicate document found: {safe_name}. Same title, category, year and file name already exist.", "warning")
                return redirect(url_for("documents", section=section))

        try:
            service = get_drive_service()
            folder_id = get_document_folder(service, section, category, company_name, year)

            for safe_name in safe_names:
                if drive_file_exists_in_folder(service, safe_name, folder_id):
                    flash(f"This file already exist: {safe_name}", "danger")
                    return redirect(request.url)

            for file, safe_name in zip(uploaded_files, safe_names):
                file.stream.seek(0)
                uploaded = upload_file_to_drive(file, folder_id, service=service)

                doc = Document(
                    section=section,
                    document_category=category,
                    document_title=title,
                    related_name=related_name,
                    year=year,
                    file_name=safe_name,
                    file_mime_type=file.mimetype,
                    file_size=request.content_length,
                    google_drive_file_id=uploaded["id"],
                    google_drive_file_link=get_drive_folder_link(folder_id),
                    google_drive_folder_id=folder_id,
                    uploaded_by=current_user.email,
                    remarks=remarks
                )
                db.session.add(doc)

            db.session.commit()
            flash(f"{len(uploaded_files)} document(s) uploaded successfully to Google Drive.", "success")
            return redirect(url_for("documents", section=section))
        except Exception as exc:
            db.session.rollback()
            flash(f"Upload failed: {exc}", "danger")
            return redirect(request.url)

    return render_template("upload.html", section=section)


@app.route("/export/<section>")
@login_required
def export_documents(section):
    if section not in SECTIONS:
        abort(404)
    if section not in user_allowed_sections(current_user):
        abort(403)

    docs = Document.query.filter_by(section=section).order_by(Document.uploaded_at.desc()).all()
    rows = []
    for index, doc in enumerate(docs, start=1):
        rows.append({
            "ID": index,
            "Section": section_label(doc.section),
            "Category": doc.document_category,
            "Document Title": doc.document_title,
            "Company Name": doc.related_name or "",
            "Year": doc.year,
            "File Name": doc.file_name,
            "Google Drive Link": get_drive_folder_link(doc.google_drive_folder_id),
            "Uploaded By": doc.uploaded_by,
            "Uploaded At": doc.uploaded_at.strftime("%d-%m-%Y %I:%M %p"),
            "Remarks": doc.remarks or ""
        })
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Documents")
    output.seek(0)
    filename = f"{section}_documents_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.route("/delete-document/<int:document_id>", methods=["POST"])
@login_required
def delete_document(document_id):
    document = Document.query.get_or_404(document_id)
    section = document.section

    if current_user.role == "viewer":
        flash("Viewer role cannot delete documents.", "danger")
        return redirect(url_for("documents", section=section))

    if section not in user_allowed_sections(current_user):
        abort(403)

    try:
        service = get_drive_service()

        if document.google_drive_file_id:
            try:
                service.files().delete(
                    fileId=document.google_drive_file_id,
                    supportsAllDrives=True
                ).execute()
            except Exception as exc:
                message = str(exc)
                if "404" not in message and "File not found" not in message:
                    raise

        folder_id = document.google_drive_folder_id
        remaining_document = Document.query.filter(
            Document.google_drive_folder_id == folder_id,
            Document.id != document.id
        ).first()

        db.session.delete(document)

        if folder_id and not remaining_document:
            try:
                folder_files = service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    spaces="drive",
                    fields="files(id, name)",
                    pageSize=1,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True
                ).execute().get("files", [])

                if not folder_files:
                    service.files().delete(
                        fileId=folder_id,
                        supportsAllDrives=True
                    ).execute()
                    DriveFolder.query.filter_by(google_drive_folder_id=folder_id).delete()
            except Exception:
                pass

        db.session.commit()
        flash("Document deleted successfully.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Delete failed: {exc}", "danger")

    return redirect(url_for("documents", section=section))


@app.route("/admin/users", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        role = request.form.get("role", "viewer").strip()

        if not name or not email or not password or role not in ROLE_LABELS:
            flash("Please enter valid user details.", "danger")
            return redirect(url_for("users"))
        if User.query.filter_by(email=email).first():
            flash("Email already exists.", "warning")
            return redirect(url_for("users"))

        user = User(name=name, email=email, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("User created successfully.", "success")
        return redirect(url_for("users"))

    all_users = User.query.order_by(User.created_at.desc()).all()
    return render_template("users.html", users=all_users)


@app.route("/health")
def health():
    return {"status": "ok", "app": "Ummid Document Management System"}


# ========================
# SAFE SQLITE -> POSTGRESQL MIGRATION
# ========================
def should_run_sqlite_migration():
    return os.environ.get("RUN_SQLITE_MIGRATION", "false").strip().lower() in ("1", "true", "yes", "y")


def get_sqlite_migration_path():
    return os.environ.get("SQLITE_MIGRATION_PATH", os.path.join(BASE_DIR, "documents.db")).strip()


def migrate_sqlite_to_current_database():
    """Safely copy old local SQLite records into the current DATABASE_URL database.
    Existing content is preserved. Duplicate records are skipped.
    """
    if not should_run_sqlite_migration():
        print("SQLite migration skipped. RUN_SQLITE_MIGRATION is not true.")
        return

    current_database_url = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if current_database_url.startswith("sqlite"):
        print("SQLite migration skipped because current database is SQLite.")
        return

    sqlite_path = get_sqlite_migration_path()
    if not os.path.exists(sqlite_path):
        print(f"SQLite migration skipped. File not found: {sqlite_path}")
        return

    try:
        import sqlite3
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = {row["name"] for row in cursor.fetchall()}

        migrated_users = 0
        migrated_folders = 0
        migrated_documents = 0

        if "user" in table_names:
            for row in cursor.execute("SELECT * FROM user"):
                email = (row["email"] or "").strip().lower()
                if email and not User.query.filter_by(email=email).first():
                    user = User(
                        name=row["name"],
                        email=email,
                        password_hash=row["password_hash"],
                        role=row["role"] or "viewer",
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow()
                    )
                    db.session.add(user)
                    migrated_users += 1

        if "drive_folder" in table_names:
            for row in cursor.execute("SELECT * FROM drive_folder"):
                existing = DriveFolder.query.filter_by(
                    section=row["section"],
                    category=row["category"],
                    year=str(row["year"]),
                    folder_name=row["folder_name"]
                ).first()
                if not existing:
                    folder = DriveFolder(
                        section=row["section"],
                        category=row["category"],
                        year=str(row["year"]),
                        folder_name=row["folder_name"],
                        google_drive_folder_id=row["google_drive_folder_id"],
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.utcnow()
                    )
                    db.session.add(folder)
                    migrated_folders += 1

        if "document" in table_names:
            for row in cursor.execute("SELECT * FROM document"):
                existing = Document.query.filter_by(
                    section=row["section"],
                    document_category=row["document_category"],
                    document_title=row["document_title"],
                    year=str(row["year"]),
                    file_name=row["file_name"]
                ).first()
                if not existing:
                    doc = Document(
                        section=row["section"],
                        document_category=row["document_category"],
                        document_title=row["document_title"],
                        related_name=row["related_name"],
                        year=str(row["year"]),
                        file_name=row["file_name"],
                        file_mime_type=row["file_mime_type"],
                        file_size=row["file_size"],
                        google_drive_file_id=row["google_drive_file_id"],
                        google_drive_file_link=row["google_drive_file_link"],
                        google_drive_folder_id=row["google_drive_folder_id"],
                        uploaded_by=row["uploaded_by"],
                        uploaded_at=datetime.fromisoformat(row["uploaded_at"]) if row["uploaded_at"] else datetime.utcnow(),
                        remarks=row["remarks"]
                    )
                    db.session.add(doc)
                    migrated_documents += 1

        db.session.commit()
        conn.close()
        print(f"SQLite migration completed. Users: {migrated_users}, Folders: {migrated_folders}, Documents: {migrated_documents}")
    except Exception as exc:
        db.session.rollback()
        print(f"SQLite migration failed but app startup will continue: {exc}")
        traceback.print_exc()


def initialize_database_safely():
    try:
        db.create_all()
        create_default_admin()
        migrate_sqlite_to_current_database()
    except Exception as exc:
        print(f"Database startup initialization failed: {exc}")
        traceback.print_exc()
        if os.environ.get("ALLOW_START_WITH_DB_ERROR", "false").strip().lower() not in ("1", "true", "yes", "y"):
            raise


with app.app_context():
    initialize_database_safely()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
