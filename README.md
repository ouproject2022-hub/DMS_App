# Ummid Document Management System

Python Flask application for uploading and managing documents on Google Drive for:

- Ummid NGO
- Mahalaxmi Trader
- Personal Documents

## Features

- Login system
- Role based access: admin, ngo, trader, personal, viewer
- Google Drive folder auto-creation
- Separate sections and categories
- Search and filter documents
- Duplicate document validation
- Excel export
- Admin user creation
- Ready for Render deployment

## Google Drive Setup

1. Create a Google Cloud project.
2. Enable Google Drive API.
3. Create a Service Account.
4. Download the service account JSON key.
5. Create a parent folder in Google Drive.
6. Share that parent folder with the service account email as Editor.
7. Copy the parent folder ID from the Google Drive URL.

Example folder URL:

```text
https://drive.google.com/drive/folders/1ABCDEF123456789
```

The folder ID is:

```text
1ABCDEF123456789
```

## Local Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open:

```text
http://127.0.0.1:10000
```

## Environment Variables

Required:

```text
SECRET_KEY
ADMIN_NAME
ADMIN_EMAIL
ADMIN_PASSWORD
GOOGLE_PARENT_FOLDER_ID
GOOGLE_CREDENTIALS
```

For Render deployment, add these in Render Environment settings.

## Default Login

Default login is created automatically from:

```text
ADMIN_EMAIL
ADMIN_PASSWORD
```

If you do not change `.env`, default login is:

```text
Email: admin@example.com
Password: Admin@12345
```

Change this before production use.

## Deploy on Render

1. Push this project to GitHub.
2. Create a new Web Service on Render.
3. Build command:

```bash
pip install -r requirements.txt
```

4. Start command:

```bash
gunicorn app:app
```

5. Add environment variables in Render.
6. Add PostgreSQL database if required and connect `DATABASE_URL`.

## Important Security Notes

- Never upload your service account JSON file to GitHub.
- Always store Google credentials in environment variables.
- Share only the required parent Drive folder with the service account.
- Change default admin password before using in production.
