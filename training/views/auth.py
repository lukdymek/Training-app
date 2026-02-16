import logging
import random

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.core.mail import EmailMessage, send_mail
from django.shortcuts import redirect, render
from django.utils import timezone

from training.models import EmailVerification, Person


RESEND_COOLDOWN_SECONDS = 60
logger = logging.getLogger(__name__)


def login_view(request):
    if request.user.is_authenticated:
        return redirect("calendar")

    if request.method == "POST":
        identifier = (request.POST.get("identifier") or "").strip()
        password = request.POST.get("password") or ""
        next_url = request.POST.get("next") or request.GET.get("next") or ""

        is_admin_login = identifier.lower() == "admin"
        looks_like_email = "@" in identifier and "." in identifier

        if not is_admin_login and not looks_like_email:
            messages.error(request, "Please enter a valid email address.")
            return render(request, "training/login.html", {"next": next_url})

        user = None

        if is_admin_login:
            user = authenticate(request, username="admin", password=password)

        if user is None and looks_like_email:
            user_model = get_user_model()
            existing_user = user_model.objects.filter(email__iexact=identifier).first()
            if existing_user:
                user = authenticate(request, username=existing_user.username, password=password)

        if user is not None:
            login(request, user)
            return redirect(next_url or "calendar")

        messages.error(request, "Invalid login details. Please try again.")

    return render(request, "training/login.html", {"next": request.GET.get("next", "")})


@login_required
def my_history(request):
    email = (request.user.email or "").strip().lower()

    if not email:
        messages.error(request, "Your account has no email address set. Please contact admin.")
        return redirect("calendar")

    person = Person.objects.filter(email__iexact=email).first()
    if not person:
        messages.error(request, "No Person record found for your email. Please contact admin.")
        return redirect("calendar")

    return redirect("person_history", person_id=person.pk)


def send_log_email(to_email: str, subject: str, body: str) -> None:
    msg = EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[to_email],
    )
    msg.send(fail_silently=False)


def _send_verification_code(email: str) -> str:
    code = f"{random.randint(0, 999999):06d}"
    try:
        logger.warning("EMAIL_BACKEND=%s DEFAULT_FROM_EMAIL=%s", settings.EMAIL_BACKEND, settings.DEFAULT_FROM_EMAIL)

        send_mail(
            subject="Your verification code",
            message=f"Your verification code is: {code}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        return code
    except Exception:
        logger.exception("Verification email send failed")
        raise


def register_request(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()

        if not email or "@" not in email:
            messages.error(request, "Please enter a valid email address.")
            return redirect("register")

        if not Person.objects.filter(email__iexact=email).exists():
            messages.error(request, "This email address is not recognized. Please contact your administrator.")
            return redirect("register")

        latest = EmailVerification.objects.filter(email=email).order_by("-created_at").first()
        if latest:
            delta = timezone.now() - latest.created_at
            if delta.total_seconds() < RESEND_COOLDOWN_SECONDS:
                wait = int(RESEND_COOLDOWN_SECONDS - delta.total_seconds())
                messages.error(request, f"Please wait {wait} seconds before requesting another code.")
                return redirect("register")

        EmailVerification.objects.filter(email=email, used_at__isnull=True).update(used_at=timezone.now())

        try:
            code = _send_verification_code(email)
        except RuntimeError as e:
            messages.error(request, str(e))
            return redirect("register")

        EmailVerification.objects.create(email=email, code=code)

        request.session["reg_email"] = email
        request.session["reg_verified"] = False
        request.session["reg_last_sent_at"] = timezone.now().isoformat()

        messages.success(request, "Verification code sent. Please check your email.")
        return redirect("register_verify")

    return render(request, "training/register.html")


def register_verify(request):
    email = (request.session.get("reg_email") or "").strip().lower()
    if not email:
        return redirect("register")

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()

        verification = EmailVerification.objects.filter(email=email, used_at__isnull=True).order_by("-created_at").first()
        if not verification:
            messages.error(request, "No active verification code found. Please request a new code.")
            return redirect("register")

        if verification.is_expired():
            messages.error(request, "Verification code expired. Please request a new code.")
            return redirect("register")

        if code != verification.code:
            messages.error(request, "Invalid code. Please try again.")
            return redirect("register_verify")

        verification.mark_used()
        request.session["reg_verified"] = True
        messages.success(request, "Code verified. Please set your password.")
        return redirect("register_set_password")

    remaining = 0
    last_sent = request.session.get("reg_last_sent_at")
    if last_sent:
        try:
            last_dt = timezone.datetime.fromisoformat(last_sent)
            if timezone.is_naive(last_dt):
                last_dt = timezone.make_aware(last_dt, timezone.get_current_timezone())
            remaining = max(0, RESEND_COOLDOWN_SECONDS - int((timezone.now() - last_dt).total_seconds()))
        except Exception:
            remaining = 0

    return render(request, "training/register_verify.html", {"email": email, "resend_remaining": remaining})


def register_resend(request):
    if request.method != "POST":
        return redirect("register_verify")

    email = (request.session.get("reg_email") or "").strip().lower()
    if not email:
        return redirect("register")

    latest = EmailVerification.objects.filter(email=email).order_by("-created_at").first()
    if latest:
        delta = timezone.now() - latest.created_at
        if delta.total_seconds() < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - delta.total_seconds())
            messages.error(request, f"Please wait {wait} seconds before resending the code.")
            return redirect("register_verify")

    EmailVerification.objects.filter(email=email, used_at__isnull=True).update(used_at=timezone.now())

    code = _send_verification_code(email)
    EmailVerification.objects.create(email=email, code=code)

    request.session["reg_last_sent_at"] = timezone.now().isoformat()
    messages.success(request, "A new verification code has been sent.")
    return redirect("register_verify")


def register_set_password(request):
    email = (request.session.get("reg_email") or "").strip().lower()
    verified = request.session.get("reg_verified") is True

    if not email or not verified:
        return redirect("register")

    if request.method == "POST":
        pw1 = request.POST.get("password1") or ""
        pw2 = request.POST.get("password2") or ""

        if len(pw1) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return redirect("register_set_password")

        if pw1 != pw2:
            messages.error(request, "Passwords do not match.")
            return redirect("register_set_password")

        user_model = get_user_model()

        user = user_model.objects.filter(email__iexact=email).first()
        if not user:
            base = email.split("@")[0]
            username = base
            i = 1
            while user_model.objects.filter(username=username).exists():
                i += 1
                username = f"{base}{i}"
            user = user_model(username=username, email=email, is_active=True, is_staff=False, is_superuser=False)

        user.password = make_password(pw1)
        user.save()

        for k in ("reg_email", "reg_verified", "reg_last_sent_at"):
            request.session.pop(k, None)

        messages.success(request, "Account created. You can now log in.")
        return redirect("login")

    return render(request, "training/register_set_password.html", {"email": email})


__all__ = [
    "login_view",
    "my_history",
    "register_request",
    "register_resend",
    "register_set_password",
    "register_verify",
]
