# Training Management Application

Internal Django web application for managing training activities, participants, trainer assignments, status workflows, reporting, and email/audit logging.

## 1. Purpose

This system is designed for operational training administration:

- Create and manage trainings
- Assign/remove trainees and trainers
- Track participant status lifecycle (`PENDING`, `AUTHORISED`, `REJECTED`, `WITHDRAWN`)
- Manage recurring training validity
- Run reporting exports (CSV)
- Capture email communications in `TrainingEmailLog`
- Manage Use of Force (UOF) assessments and DOCX exports

## 2. Tech Stack

- Python `3.12.3` (`runtime.txt`)
- Django `6.0.1`
- PostgreSQL via `psycopg` + `dj-database-url`
- Gunicorn
- WhiteNoise for static files
- `django-auditlog` for admin/model audit trail
- `django-anymail` (available; currently not used for outbound in compose flow)

## 3. Repository Structure

- `config/` Django project config (`settings.py`, `urls.py`, `wsgi.py`)
- `training/` main app
  - `models.py` core domain and logging models
  - `views/` split by domain (`auth`, `calendar`, `trainings`, `trainers`, `people`, `reports`, `emails`, `uof`)
  - `templates/training/` UI templates
  - `static/` app static assets, including UOF DOCX template
  - `management/commands/` import/seed/admin utility commands

## 4. Main Functional Areas

- Calendar and training list/detail
- Training CRUD and participant operations
- Trainer skill management and trainer day tracking
- Person history and search APIs
- Reports:
  - per person
  - trainer-days monthly
- Email compose/logging:
  - participant and admin-group modes
  - **currently configured as log-only behavior in compose flow**
- UOF standards, score entry, results, and DOCX export (single + ZIP)

## 5. Security and Access Model

- Authentication: Django auth
- Authorization:
  - Many operational endpoints guarded by `@login_required` + `staff_required`
  - Unauthorized access raises HTTP 403 (`training.views.custom_403`)
- CSRF protection: enabled via Django middleware
- Clickjacking protection: Django middleware enabled
- Secret management via environment variables (see section 8)
- Production host and CSRF origin whitelisting via env vars

## 6. Data Model Overview

Key models in `training/models.py`:

- `Person`
- `Subject`
- `TrainerSkill`
- `Training`
- `Participation`
- `EmailVerification`
- `TrainingEmailLog`
- `EmailTemplate`
- `EmailRecipient` / `EmailRecipientGroup`
- `UseOfForceStandard`, `UofAssessment`

Indexes and uniqueness constraints are defined in migrations for core lookups and duplicate prevention.

## 7. Runtime / Deployment

### 7.1 Process

`Procfile`:

```procfile
web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT
```

### 7.2 WSGI Entry

- `config.wsgi:application`

### 7.3 Static Files

- `STATIC_ROOT = BASE_DIR / "staticfiles"`
- WhiteNoise compressed manifest storage enabled

## 8. Environment Variables

Configured in `config/settings.py`.

Required/important:

- `SECRET_KEY`
- `DEBUG` (`0` or `1`)
- `ALLOWED_HOSTS` (comma-separated in production)
- `CSRF_TRUSTED_ORIGINS` (comma-separated)
- `DATABASE_URL` (required on Railway environment)

Local PostgreSQL fallback (if `DATABASE_URL` is not set):

- `PGDATABASE`
- `PGUSER`
- `PGPASSWORD`
- `PGHOST`
- `PGPORT`

Email backend options:

- `EMAIL_BACKEND`
- SMTP mode:
  - `EMAIL_HOST`
  - `EMAIL_PORT`
  - `EMAIL_HOST_USER`
  - `EMAIL_HOST_PASSWORD`
  - `EMAIL_USE_TLS`
- Optional Anymail key:
  - `SENDINBLUE_API_KEY`
- `DEFAULT_FROM_EMAIL`

## 9. Local Setup

1. Create and activate venv
2. Install dependencies
3. Configure environment variables (`.env` for local)
4. Run migrations
5. Run server

Example:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

## 10. Operational Commands

Common management commands:

- `python manage.py check`
- `python manage.py migrate`
- `python manage.py collectstatic --noinput`
- `python manage.py import_people`
- `python manage.py seed_use_of_force`
- `python manage.py seed_uof_standards`
- `python manage.py create_users_from_people`

## 11. Current Email Behavior (Important)

For training compose flows, email actions are currently **log-only** by design:

- Records are created in `TrainingEmailLog`
- Compose flow does not perform external send attempts
- This behavior was intentionally kept for restricted hosting environments

Registration verification still uses Django email backend settings if enabled.

## 12. IT Review Checklist

- Validate environment variable management policy
- Confirm PostgreSQL network/access controls
- Configure `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` for target domains
- Set `DEBUG=0` in production
- Confirm static file path/storage behavior
- Review backup/restore strategy for PostgreSQL
- Review audit log retention and PII/data governance requirements
- Confirm TLS termination and reverse proxy headers at platform level

## 13. Notes

- The codebase has been refactored to modular view files under `training/views/`.
- Legacy monolithic view file has been removed from active routing.
- This README is intended for internal deployment and security review context.

