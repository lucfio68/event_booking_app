# config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'postgresql://username:password@ep-host.neon.tech/neondb?sslmode=require'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    MAIL_SERVER = os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', '1', 'yes']
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME') or 'your-email@gmail.com'
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') or 'your-app-password'
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER')
    RESEND_API_KEY = os.environ.get('RESEND_API_KEY') or ''
    RESEND_FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL') or 'onboarding@resend.dev' or 'EventBooking <prenotazionilacorrente@gmail.com.com>'
    BREVO_API_KEY = os.environ.get('BREVO_API_KEY') or ''
    BREVO_FROM_EMAIL = os.environ.get('BREVO_FROM_EMAIL') or ''

    # Chiave segreta richiesta (oltre al login admin) per le route di manutenzione DB
    # (/init-db, /admin/migrate-layout-posti). Se non impostata, quelle route restano bloccate.
    MIGRATION_SECRET = os.environ.get('MIGRATION_SECRET') or ''

    # === Google Calendar (Fase B) ===
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID') or ''
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET') or ''
    GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_REDIRECT_URI') or ''
    # Chiave Fernet (32 byte urlsafe-base64) per cifrare il refresh_token nel DB.
    # Generarla una volta con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    TOKEN_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY') or ''