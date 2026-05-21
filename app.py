import os
import io
import json
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_file, abort
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_bcrypt import Bcrypt
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from google.oauth2 import service_account
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

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "doc", "docx", "xls", "xlsx", "csv", "txt"}
SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_PARENT_FOLDER_ID = os.environ.get("GOOGLE_PARENT_FOLDER_ID", "").strip()

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


def get_drive_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS environment variable is missing.")

    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_CREDENTIALS is not valid JSON.") from exc

    if "private_key" in creds_dict:
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


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
        pageSize=1
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def create_folder(service, name, parent_id=None):
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def find_or_create_folder(service, name, parent_id=None):
    folder_id = find_folder(service, name, parent_id)
    if folder_id:
        return folder_id
    return create_folder(service, name, parent_id)


def get_document_folder(service, section, category, year):
    if not GOOGLE_PARENT_FOLDER_ID:
        raise RuntimeError("GOOGLE_PARENT_FOLDER_ID environment variable is missing.")

    root_id = GOOGLE_PARENT_FOLDER_ID
    section_name = section_label(section)
    section_id = find_or_create_folder(service, section_name, root_id)
    category_id = find_or_create_folder(service, category, section_id)
    year_id = find_or_create_folder(service, str(year), category_id)

    existing = DriveFolder.query.filter_by(
        section=section,
        category=category,
        year=str(year)
    ).first()
    if not existing:
        db.session.add(DriveFolder(
            section=section,
            category=category,
            year=str(year),
            folder_name=f"{section_name}/{category}/{year}",
            google_drive_folder_id=year_id
        ))
        db.session.commit()
    elif existing.google_drive_folder_id != year_id:
        existing.google_drive_folder_id = year_id
        db.session.commit()

    return year_id


def upload_file_to_drive(file_storage, folder_id):
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
        fields="id, webViewLink"
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
        "current_year": datetime.now().year
    }


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
    years = [row[0] for row in db.session.query(Document.year).filter_by(section=section).distinct().order_by(Document.year.desc()).all()]
    return render_template("documents.html", docs=docs, section=section, years=years)


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
        related_name = request.form.get("related_name", "").strip()
        year = request.form.get("year", str(datetime.now().year)).strip()
        remarks = request.form.get("remarks", "").strip()
        file = request.files.get("file")

        if not category or not title or not year:
            flash("Category, title and year are required.", "danger")
            return redirect(request.url)
        if category not in SECTIONS[section]["categories"]:
            flash("Invalid category selected.", "danger")
            return redirect(request.url)
        if not file or not file.filename:
            flash("Please select a file.", "danger")
            return redirect(request.url)
        if not allowed_file(file.filename):
            flash("File type is not allowed.", "danger")
            return redirect(request.url)

        safe_name = secure_filename(file.filename)
        duplicate = Document.query.filter_by(
            section=section,
            document_category=category,
            document_title=title,
            year=year,
            file_name=safe_name
        ).first()
        if duplicate:
            flash("Duplicate document found. Same title, category, year and file name already exist.", "warning")
            return redirect(url_for("documents", section=section))

        try:
            service = get_drive_service()
            folder_id = get_document_folder(service, section, category, year)
            # Upload uses a fresh stream, because get_document_folder uses only metadata.
            uploaded = upload_file_to_drive(file, folder_id)

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
                google_drive_file_link=uploaded.get("webViewLink", f"https://drive.google.com/file/d/{uploaded['id']}/view"),
                google_drive_folder_id=folder_id,
                uploaded_by=current_user.email,
                remarks=remarks
            )
            db.session.add(doc)
            db.session.commit()
            flash("Document uploaded successfully to Google Drive.", "success")
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
            "Related Name": doc.related_name or "",
            "Year": doc.year,
            "File Name": doc.file_name,
            "Google Drive Link": doc.google_drive_file_link,
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


with app.app_context():
    db.create_all()
    create_default_admin()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
