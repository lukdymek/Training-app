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
  - Login is required for all application pages and APIs (except login/register/logout endpoints).
  - Regular users can access only:
    - `calendar`
    - `my history` (their own record)
  - Staff users can access operational areas.
  - UOF standards table is restricted to superusers.
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
- `python manage.py import_people <path_to_people.xlsx>`
- `python manage.py assign_contingent_from_deployment --only-empty`
- `python manage.py seed_uof_standards`
- `python manage.py create_users_from_people` (optional; accounts can also be created during self-registration)
- `python manage.py load_email_templates`
- `python manage.py seed_use_of_force` (dev/demo dummy data only)

### 10.1 Fresh Database Bootstrap (handoff procedure)

Use this sequence when handing the app to a new environment with an empty database:

1. Apply schema:
   - `python manage.py migrate`
2. Create initial admin account:
   - `python manage.py createsuperuser`
3. Import people source file (Excel `.xlsx` export):
   - `python manage.py import_people <path_to_people.xlsx>`
4. Populate `Person.contingent` from deployment names:
   - `python manage.py assign_contingent_from_deployment --only-empty`
5. Seed UOF evaluation thresholds:
   - `python manage.py seed_uof_standards`
6. Optional pre-provisioning of auth users:
   - `python manage.py create_users_from_people`

Notes:



### 10.2 First-Day Admin Checklist

After bootstrap, run this quick validation:

1. Login with the superuser account.
2. Verify people import:
   - Open People list and confirm records are present.
   - Spot-check a few `sysper_id`, email, deployment values.
3. Verify contingent mapping:
   - Confirm `contingent` is populated for expected people.
4. Verify UOF standards:
   - Open UOF standards page as superuser and confirm rows exist for both genders and all age groups.
5. Verify role-based access:
   - Test with a regular user account:
     - can access calendar
     - can access own `my history`
     - cannot access staff pages (trainings, people list, reports, UOF admin pages)
6. Verify registration flow:
   - Use an email that exists in `Person`.
   - Complete code verification + password set.
   - Confirm login works.
7. Verify one end-to-end training action as staff:
   - create a training
   - add one trainee
   - update participation status
8. Verify reporting/export:
   - open person report and export CSV
   - open trainer-days report and export CSV

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

## 14. Docker Handoff

This repository includes:

- `Dockerfile`
- `.dockerignore`
- `docker-compose.yml` (app + PostgreSQL for local/IT validation)

### 14.1 Build and run (single container)

1. Build image:
   - `docker build -t training-app:latest .`
2. Run container with env file:
   - `docker run --rm -p 8000:8000 --env-file .env training-app:latest`

### 14.2 Build and run (docker compose)

1. Start stack:
   - `docker compose up --build -d`
2. Check logs:
   - `docker compose logs -f web`
3. Stop stack:
   - `docker compose down`

### 14.3 First-time DB bootstrap in Docker

After containers are up:

1. Create admin:
   - `docker compose exec web python manage.py createsuperuser`
2. Copy people file into web container (example):
   - `docker cp ./people.xlsx training-web:/tmp/people.xlsx`
3. Import people (CLI option):
   - docker compose exec web python manage.py import_people /tmp/people.xlsx
   - Alternative (admin UI): Login as superuser -> Persons -> Import from Excel
4. Fill contingent:
   - `docker compose exec web python manage.py assign_contingent_from_deployment --only-empty`
5. Seed UOF standards:
   - `docker compose exec web python manage.py seed_uof_standards`
6. Load email templates:
   - 'docker compose exex web python manage.py load_email_templates'

### 14.4 Production notes

- Replace sample secrets/passwords in `docker-compose.yml`.
- Set `DEBUG=0`.
- Set real `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
- Use managed PostgreSQL or secure volume backup policy.
- Do not commit real `.env` values.
### 14.5 Reviewer Handoff Checklist

If you want others to validate the app quickly, share:

- GitHub repository URL (full codebase)
- A .env.example file with safe sample values (no real secrets)
- These exact startup commands:
  - docker compose up --build
  - docker compose exec web python manage.py createsuperuser

Optional bootstrap data commands:

- docker compose exec web python manage.py import_people /tmp/people.xlsx
- docker compose exec web python manage.py assign_contingent_from_deployment --only-empty
- docker compose exec web python manage.py seed_uof_standards
- docker compose exec web python manage.py load_email_templates
