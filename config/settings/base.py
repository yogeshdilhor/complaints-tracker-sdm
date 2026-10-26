from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "core",
    "complaints",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.ExpiredLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "complaints.context_processors.nav",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = ["core.backends.ExpiringModelBackend"]
# Logins made for department officers last this many days, then stop working.
OFFICER_LOGIN_DAYS = env.int("OFFICER_LOGIN_DAYS", default=30)
# Most officer logins that can be active at once; 0 = no limit.
MAX_OFFICER_LOGINS = env.int("MAX_OFFICER_LOGINS", default=0)
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "complaints:list"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded documents are never exposed at a public URL; see complaints.views.document_download.
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))

ADMIN_URL = env("ADMIN_URL", default="admin/")

DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

# Ageing buckets by calendar days at the current office; the highest matching threshold wins.
AGEING_THRESHOLDS = {"safe": 0, "warning": 3, "overdue": 7, "critical": 10}

# Deadline (days) a portal's own norms give for the department's report; shortens the SLA.
PLATFORM_SLA_DAYS = {"samadhan_shivir": 7}

PAGE_SIZE = 25

# Intake runs OCR on image-only PDF pages (scans, photos, screenshots), at most this many per file.
INTAKE_OCR_MAX_PAGES = 6

# Optional AI reading of typed letters and reports. No key for the chosen provider = off.
# LLM_PROVIDER is "openai" or "sarvam"; change it (and restart) to switch.
LLM_PROVIDER = env("LLM_PROVIDER", default="openai")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_MODEL = env("OPENAI_MODEL", default="gpt-6-luna")
# Model used when you choose "ChatGPT reads the page images" at upload (needs to see images).
OPENAI_VISION_MODEL = env("OPENAI_VISION_MODEL", default="gpt-4.1-mini")
VISION_MAX_PAGES = env.int("VISION_MAX_PAGES", default=6)
# Claude (Anthropic), one of the models that can analyse a long legacy file.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", default="claude-sonnet-5-5")
# Legacy complaints: big scanned files, read in the background.
LEGACY_MAX_MB = env.int("LEGACY_MAX_MB", default=150)
LEGACY_MAX_PAGES = env.int("LEGACY_MAX_PAGES", default=150)
LEGACY_RUN_INLINE = env.bool("LEGACY_RUN_INLINE", default=False)  # tests run it in the request
OPENAI_REASONING_EFFORT = env("OPENAI_REASONING_EFFORT", default="")  # "" = model default
SARVAM_API_KEY = env("SARVAM_API_KEY", default="")
SARVAM_MODEL = env("SARVAM_MODEL", default="sarvam-105b")
SARVAM_REASONING_EFFORT = env("SARVAM_REASONING_EFFORT", default="off")  # off = no thinking
# Scanned pages read below this Tesseract confidence (0-100) are treated as handwriting.
TYPED_MIN_OCR_CONFIDENCE = env.int("TYPED_MIN_OCR_CONFIDENCE", default=70)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"complaints": {"handlers": ["console"], "level": "INFO"}},
}
