import os
import re
import io
import threading
import socket
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort, session, Response
)
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from flask_mail import Mail, Message
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import func, or_, text, inspect
from sqlalchemy.orm import joinedload
from itsdangerous import URLSafeTimedSerializer

import requests as http_requests
from cryptography.fernet import Fernet, InvalidToken
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as google_build
from googleapiclient.errors import HttpError as GoogleHttpError

from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas as pdfcanvas

from models import (
    db, Utente, Sala, Evento, Prenotazione, Posto,
    GenereEvento, LayoutPosti, GoogleConnessione,
    CalendarioGoogle, Gestore
)
from config import Config


# ==============================================================================
# 1. INIZIALIZZAZIONE APPLICAZIONE ED ESTENSIONI
# ==============================================================================

app = Flask(__name__)
app.config.from_object(Config)

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_size': 5,
    'max_overflow': 10,
    'pool_timeout': 30
}

db.init_app(app)
mail = Mail(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Effettua il login per accedere a questa pagina.'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Utente, int(user_id))


# ==============================================================================
# 2. UTILITY DI SICUREZZA E TOKEN
# ==============================================================================

def get_reset_token(email):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='password-reset-salt')


def verify_reset_token(token, max_age=3600):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        return serializer.loads(token, salt='password-reset-salt', max_age=max_age)
    except Exception:
        return None


# ==============================================================================
# 3. SISTEMA EMAIL (SMTP -> BREVO -> RESEND FALLBACK + TASK ASINCRONI)
# ==============================================================================

_email_queue = []  # Queue in memoria per retry


class EmailNetworkError(Exception):
    pass


def _extract_email(raw):
    """Estrae l'indirizzo email da stringhe tipo 'Nome <email@dom.com>'."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    m = re.search(r'<([^>]+)>', raw)
    email = m.group(1).strip() if m else raw
    if '@' not in email or '.' not in email.split('@')[-1]:
        return None
    return email


def _is_network_error(e):
    """Riconosce errori di rete comuni su Render free tier."""
    msg = str(e).lower()
    network_errors = [
        'network is unreachable', 'no route to host', 'connection refused',
        'connection timed out', 'name or service not known', 'temporary failure in name resolution',
        'errno 101', 'errno 111', 'errno 113', 'errno -2', 'errno -3',
        'ssl', 'tls', 'authentication', 'smtplib'
    ]
    return any(err in msg for err in network_errors)


def _send_via_brevo(msg, api_key):
    """Invia email tramite l'API HTTP di Brevo."""
    from_email = app.config.get('BREVO_FROM_EMAIL') or _extract_email(app.config.get('MAIL_DEFAULT_SENDER'))
    if not from_email:
        raise EmailNetworkError('BREVO_FROM_EMAIL non configurato')

    recipients = msg.recipients if isinstance(msg.recipients, list) else [msg.recipients]
    payload = {
        "sender": {"email": from_email, "name": "EventBooking"},
        "to": [{"email": r} for r in recipients],
        "subject": msg.subject,
        "textContent": msg.body or ''
    }
    if msg.html:
        payload["htmlContent"] = msg.html

    resp = http_requests.post(
        'https://api.brevo.com/v3/smtp/email',
        headers={
            'api-key': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        json=payload,
        timeout=10
    )
    if resp.status_code in (200, 201, 202):
        return True
    raise EmailNetworkError(f'Brevo HTTP {resp.status_code}: {resp.text[:300]}')


def _send_via_resend(msg, api_key):
    """Invia email tramite l'API HTTP di Resend."""
    resend_from = app.config.get('RESEND_FROM_EMAIL')
    if not resend_from:
        extracted = _extract_email(msg.sender or app.config.get('MAIL_DEFAULT_SENDER'))
        if extracted and extracted.split('@')[-1] not in ('resend.dev',):
            resend_from = 'onboarding@resend.dev'
        else:
            resend_from = extracted or 'onboarding@resend.dev'

    payload = {
        "from": resend_from,
        "to": msg.recipients if isinstance(msg.recipients, list) else [msg.recipients],
        "subject": msg.subject,
        "text": msg.body or ''
    }
    if msg.html:
        payload["html"] = msg.html

    resp = http_requests.post(
        'https://api.resend.com/emails',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json=payload,
        timeout=10
    )
    if resp.status_code in (200, 201, 202):
        return True
    raise EmailNetworkError(f'Resend HTTP {resp.status_code}: {resp.text[:300]}')


def send_email_message(msg):
    """Invia email: SMTP -> Brevo (primario per Render) -> Resend (fallback)."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        mail.send(msg)
        socket.setdefaulttimeout(old_timeout)
        return True
    except Exception as smtp_err:
        socket.setdefaulttimeout(old_timeout)
        if not _is_network_error(smtp_err):
            raise

        errors = [f'SMTP: {smtp_err}']

        brevo_key = app.config.get('BREVO_API_KEY')
        if brevo_key:
            try:
                return _send_via_brevo(msg, brevo_key)
            except Exception as brevo_err:
                errors.append(f'Brevo: {brevo_err}')
        else:
            errors.append('Brevo: BREVO_API_KEY non configurata')

        resend_key = app.config.get('RESEND_API_KEY')
        if resend_key:
            try:
                return _send_via_resend(msg, resend_key)
            except Exception as resend_err:
                errors.append(f'Resend: {resend_err}')
        else:
            errors.append('Resend: RESEND_API_KEY non configurata')

        raise EmailNetworkError(' | '.join(errors))


def _graceful_send_email(application, fn, *args, **kwargs):
    """Wrapper di contesto che cattura gli errori di rete in modo asincrono."""
    with application.app_context():
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5)
        try:
            fn(*args, **kwargs)
            application.logger.info(f'Email inviata correttamente: {fn.__name__}')
        except Exception as e:
            if _is_network_error(e):
                application.logger.info(f'Email non inviata (rete non disponibile): {fn.__name__} — {e}')
                _email_queue.append({'fn': fn.__name__, 'args': args, 'kwargs': kwargs, 'error': str(e)})
            else:
                application.logger.error(f'Errore invio email async: {e}')
        finally:
            socket.setdefaulttimeout(old_timeout)
            db.session.remove()


def run_email_task(application, fn, *args, **kwargs):
    """Esegue l'invio email in un thread separato daemon."""
    thread = threading.Thread(
        target=_graceful_send_email,
        args=(application, fn) + args,
        kwargs=kwargs,
        daemon=True
    )
    thread.start()


def _send_registration_email(utente_id):
    with db.session.no_autoflush:
        utente = db.session.get(Utente, utente_id)
        if not utente:
            return
        try:
            msg = Message(
                subject='Benvenuto su EventBooking - Registrazione completata',
                recipients=[utente.email],
                sender='EventBooking <noreply@event_booking.com>',
                body=f"""Ciao {utente.nome_cognome},

Benvenuto su EventBooking!

La tua registrazione e' stata completata con successo.

Ecco i tuoi dati:
- Username: {utente.username}
- Email: {utente.email}
- Nome: {utente.nome_cognome}

Puoi ora accedere all'applicazione e prenotare i posti per gli eventi.

Grazie per esserti registrato!
"""
            )
            send_email_message(msg)
        except Exception as e:
            app.logger.error(f'Errore invio email registrazione: {e}')


def _send_registration_notify_admin(utente_id):
    with db.session.no_autoflush:
        utente = db.session.get(Utente, utente_id)
        if not utente:
            return
        try:
            admin = Utente.query.filter_by(tipo='admin').first()
            if admin:
                msg = Message(
                    subject=f'Nuova Registrazione - {utente.nome_cognome}',
                    recipients=[admin.email],
                    sender='EventBooking <noreply@event_booking.com>',
                    body=f"""Nuovo utente registrato su EventBooking:

Nome: {utente.nome_cognome}
Username: {utente.username}
Email: {utente.email}
Cellulare: {utente.cellulare or 'Non fornito'}
Data registrazione: {utente.data_registrazione.strftime('%d/%m/%Y %H:%M')}

L'utente puo' ora effettuare il login e prenotare posti.
"""
                )
                send_email_message(msg)
        except Exception as e:
            app.logger.error(f'Errore notifica admin: {e}')


def _send_confirmation_email(evento_id, utente_id, posti_ids, nome_prenotazione=None):
    with db.session.no_autoflush:
        evento = db.session.get(Evento, evento_id)
        utente = db.session.get(Utente, utente_id)
        if not evento or not utente:
            return
        posti = db.session.query(Posto).filter(Posto.id.in_(posti_ids)).all() if posti_ids else []
        posti_str = ', '.join([f"{p.fila}{p.colonna}" for p in posti])
        display_name = nome_prenotazione or utente.nome_cognome
        num_posti = len(posti)
        posti_label = "posto" if num_posti == 1 else "posti"

        try:
            msg_user = Message(
                subject=f'Conferma Prenotazione - {num_posti} {posti_label} - {evento.nome}',
                recipients=[utente.email],
                sender='EventBooking <noreply@event_booking.com>',
                body=f"""Ciao {display_name},

La tua prenotazione per l'evento "{evento.nome}" e' stata confermata.

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti prenotati ({num_posti}): {posti_str}

Grazie!
"""
            )
            send_email_message(msg_user)
        except Exception as e:
            app.logger.error(f'Errore email conferma utente: {e}')

        try:
            if evento.sala.email_admin:
                admin_emails = [e.strip() for e in evento.sala.email_admin.split(',') if e.strip()]
                if admin_emails:
                    msg_admin = Message(
                        subject=f'Nuova Prenotazione - {num_posti} {posti_label} - {evento.nome}',
                        recipients=admin_emails,
                        sender='EventBooking <noreply@event_booking.com>',
                        body=f"""Nuova prenotazione confermata:

Evento: {evento.nome}
Data: {evento.data_evento.strftime('%d/%m/%Y')}
Utente: {display_name} ({utente.email})
Posti prenotati ({num_posti}): {posti_str}
"""
                    )
                    send_email_message(msg_admin)
        except Exception as e:
            app.logger.error(f'Errore email conferma admin: {e}')


def _send_cancellation_email(evento_id, utente_id, posti_str, prenotazione_eliminata=False, nome_prenotazione=None):
    with db.session.no_autoflush:
        evento = db.session.get(Evento, evento_id)
        utente = db.session.get(Utente, utente_id)
        if not evento or not utente:
            return
        display_name = nome_prenotazione or utente.nome_cognome
        num_posti = len([p.strip() for p in posti_str.split(',') if p.strip()]) if posti_str else 0
        posti_label = "posto" if num_posti == 1 else "posti"

        try:
            if prenotazione_eliminata:
                subject = f'Prenotazione Annullata - {num_posti} {posti_label} - {evento.nome}'
                body = f"""Ciao {display_name},

La tua prenotazione per l'evento "{evento.nome}" e' stata annullata (tutti i posti rimossi).

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti annullati ({num_posti}): {posti_str}

Se non hai richiesto tu questa operazione, contatta l'amministratore.
"""
            else:
                subject = f'Posti Annullati - {num_posti} {posti_label} - {evento.nome}'
                body = f"""Ciao {display_name},

I posti {posti_str} per l'evento "{evento.nome}" sono stati annullati.

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti annullati ({num_posti}): {posti_str}

Se non hai richiesto tu questa operazione, contatta l'amministratore.
"""
            msg = Message(
                subject=subject,
                recipients=[utente.email],
                sender='EventBooking <noreply@event_booking.com>',
                body=body
            )
            send_email_message(msg)
        except Exception as e:
            app.logger.error(f'Errore email cancellazione: {e}')


def _send_reset_password_email(utente_id, reset_url):
    with db.session.no_autoflush:
        utente = db.session.get(Utente, utente_id)
        if not utente:
            return
        try:
            msg = Message(
                subject='Reset Password EventBooking',
                recipients=[utente.email],
                sender='EventBooking <noreply@event_booking.com>',
                body=f"""Ciao {utente.nome_cognome},

Hai richiesto il reset della password.

Clicca sul link seguente per reimpostarla:
{reset_url}

Il link scade tra 1 ora.

Se non hai richiesto tu questa operazione, ignora questa email.
"""
            )