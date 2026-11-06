"""Django settings for Printmast POD platform."""
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY', default='dev-secret-change-me')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # 3rd party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    'drf_spectacular',

    # Local apps
    'apps.core',
    'apps.accounts',
    'apps.catalog',
    'apps.orders',
    'apps.designs',
    'apps.payments',
    'apps.shipping',
    'apps.production',
    'apps.reship',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

DATABASES = {
    'default': env.db('DATABASE_URL', default='sqlite:///' + str(BASE_DIR / 'db.sqlite3'))
}

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ── File storage (Cloudflare R2 when configured, local disk in dev) ───────────

_R2_KEY = env('R2_ACCESS_KEY_ID', default='')

if _R2_KEY:
    STORAGES = {
        'default': {
            'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
            'OPTIONS': {
                'access_key':       _R2_KEY,
                'secret_key':       env('R2_SECRET_ACCESS_KEY'),
                'bucket_name':      env('R2_BUCKET_NAME', default='fulfillment'),
                'endpoint_url':     env('R2_ENDPOINT_URL'),
                'region_name':      'auto',
                'querystring_auth': True,   # presigned URLs — no public bucket needed
                'querystring_expire': 86400,  # 24 h
                'file_overwrite':   False,
            },
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    }
    MEDIA_URL = ''   # URLs are absolute presigned; MEDIA_URL prefix not used
else:
    MEDIA_URL  = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=12),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Printmast API',
    'DESCRIPTION': 'Printmast POD platform — backend API',
    'VERSION': '0.1.0',
}

CORS_ALLOWED_ORIGINS = env.list('CORS_ALLOWED_ORIGINS', default=['http://localhost:5173'])
CORS_ALLOW_CREDENTIALS = True

# Field-level encryption
FIELD_ENCRYPTION_KEY = env('FIELD_ENCRYPTION_KEY', default='')

# ShipStation (legacy global key — per-account keys stored in DB)
SHIPSTATION_BASE_URL = env('SHIPSTATION_BASE_URL', default='https://api.shipstation.com')
# Public URL used to build ShipStation webhook callback URLs
SITE_URL = env('SITE_URL', default='http://localhost:8000')

# Celery
from celery.schedules import crontab  # noqa: E402

CELERY_BROKER_URL = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULE = {
    # Primary: ShipStation webhook handles real-time updates.
    # This periodic task is the fallback — catches anything the webhook missed.
    'sync-all-shipstation-accounts': {
        'task': 'apps.shipping.tasks.sync_all_active_accounts',
        'schedule': 600,  # every 10 minutes
    },
    # Monthly billing cycle: generate print-center + sub-user invoices for the
    # month that just ended, and apply any pending timezone cutover.
    'generate-monthly-invoices': {
        'task': 'apps.accounts.tasks.generate_monthly_invoices',
        'schedule': crontab(day_of_month=1, hour=0, minute=5),
    },
    # 5 days' grace period, then anything still unpaid becomes overdue.
    'mark-overdue-invoices': {
        'task': 'apps.accounts.tasks.mark_overdue_invoices',
        'schedule': crontab(day_of_month=6, hour=0, minute=5),
    },
}
