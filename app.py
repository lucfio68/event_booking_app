import os
import re
import unicodedata
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from itsdangerous import URLSafeTimedSerializer

import requests as http_requests
from cryptography.fernet import Fernet, InvalidToken
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build as google_build
from googleapiclient.errors import HttpError as GoogleHttpError

from urllib.parse import urlparse
from reportlab.lib.pagesizes import A3, A4, landscape, portrait
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth

from models import (
    db, Utente, Sala, Evento, Prenotazione, Posto,
    GenereEvento, LayoutPosti, GoogleConnessione,
    CalendarioGoogle, Gestore, Stagione,
    CategoriaRipetitiva, PrenotazioneRipetitiva, EsclusioneRipetitiva
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
            send_email_message(msg)
        except Exception as e:
            app.logger.error(f'Errore email reset password: {e}')


def _send_deletion_emails(email_data_list):
    for data in email_data_list:
        try:
            msg = Message(
                subject=data['subject'],
                recipients=[data['recipient']],
                sender='EventBooking <noreply@event_booking.com>',
                body=data['body']
            )
            send_email_message(msg)
        except Exception as e:
            app.logger.error(f'Errore email cancellazione posti: {e}')


# ==============================================================================
# 4. AUTENTICAZIONE E ACCOUNT UTENTE
# ==============================================================================

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        nome = request.form.get('nome_cognome', '').strip()
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        cellulare = request.form.get('cellulare', '').strip()
        password = request.form.get('password', '')

        if not nome or not username or not email or not password:
            flash('Tutti i campi obbligatori devono essere compilati.', 'danger')
            return redirect(url_for('register'))
        if len(password) < 8:
            flash('La password deve essere di almeno 8 caratteri.', 'danger')
            return redirect(url_for('register'))
        if '@' not in email or '.' not in email.split('@')[-1]:
            flash('Inserisci un indirizzo email valido.', 'danger')
            return redirect(url_for('register'))
        if not username.isalnum():
            flash('Lo username deve contenere solo lettere e numeri.', 'danger')
            return redirect(url_for('register'))
        if len(username) < 3:
            flash('Lo username deve essere di almeno 3 caratteri.', 'danger')
            return redirect(url_for('register'))

        if Utente.query.filter_by(email=email).first():
            flash("Email gia' registrata.", 'danger')
            return redirect(url_for('register'))
        if Utente.query.filter_by(username=username).first():
            flash("Username gia' in uso.", 'danger')
            return redirect(url_for('register'))

        user = Utente(nome_cognome=nome, username=username, email=email, cellulare=cellulare, tipo='user')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        run_email_task(app, _send_registration_email, user.id)
        run_email_task(app, _send_registration_notify_admin, user.id)

        flash('Registrazione completata! Effettua il login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        app.logger.info(f'LOGIN DEBUG: identifier={identifier!r} len_password={len(password)}')

        if '@' in identifier:
            user = Utente.query.filter_by(email=identifier).first()
        else:
            user = Utente.query.filter_by(username=identifier).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('calendar_view'))
        flash('Credenziali non valide.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = Utente.query.filter_by(email=email).first()
        if not user:
            flash("Se l'indirizzo e' registrato, riceverai un'email con le istruzioni.", 'info')
            return redirect(url_for('login'))

        token = get_reset_token(user.email)
        reset_url = url_for('reset_password', token=token, _external=True)

        run_email_task(app, _send_reset_password_email, user.id, reset_url)

        flash('Email di reset inviata! Controlla la tua casella di posta.', 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_reset_token(token)
    if not email:
        flash('Link di reset non valido o scaduto.', 'danger')
        return redirect(url_for('forgot_password'))

    user = Utente.query.filter_by(email=email).first()
    if not user:
        flash('Utente non trovato.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if len(password) < 8:
            flash('La password deve essere di almeno 8 caratteri.', 'danger')
            return redirect(url_for('reset_password', token=token))
        if password != confirm:
            flash('Le password non coincidono.', 'danger')
            return redirect(url_for('reset_password', token=token))
        user.set_password(password)
        db.session.commit()
        flash('Password reimpostata con successo! Effettua il login.', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password_form.html', token=token)


# ==============================================================================
# 5. CALENDARIO ED EVENTI LATO UTENTE
# ==============================================================================

@app.route('/')
@app.route('/calendar')
@login_required
def calendar_view():
    return render_template('calendar.html')


@app.route('/api/events')
@login_required
def api_events():
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    if not year or not month:
        return jsonify({'error': 'Anno e mese richiesti'}), 400

    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)

    eventi = Evento.query.filter(
        Evento.data_evento >= start_date,
        Evento.data_evento < end_date,
        Evento.cancellato_google == False
    ).all()

    result = {}
    for ev in eventi:
        day = ev.data_evento.day
        if day not in result:
            result[day] = []
        result[day].append({
            'id': ev.id,
            'nome': ev.nome,
            'ora': ev.ora_inizio.strftime('%H:%M'),
            'sala': ev.sala.nome
        })
    return jsonify(result)


@app.route('/api/event/<int:event_id>')
@login_required
def api_event_detail(event_id):
    ev = db.session.get(Evento, event_id)
    if not ev:
        abort(404)
    posti_prenotati = Posto.query.filter_by(evento_id=event_id, stato='prenotato').count()
    return jsonify({
        'id': ev.id,
        'nome': ev.nome,
        'descrizione': ev.descrizione,
        'data': ev.data_evento.strftime('%Y-%m-%d'),
        'ora': ev.ora_inizio.strftime('%H:%M'),
        'durata': ev.durata,
        'sala': ev.sala.nome,
        'posti_max': ev.posti_max,
        'posti_prenotati': posti_prenotati,
        'file': ev.file,
        'colonne': ev.colonne
    })


@app.route('/le-mie-prenotazioni')
@login_required
def mie_prenotazioni():
    oggi = date.today()
    prenotazioni = (
        Prenotazione.query
        .join(Evento)
        .filter(Prenotazione.utente_id == current_user.id)
        .all()
    )

    def posti_label(p):
        posti_ordinati = sorted(p.posti, key=lambda po: (po.fila, po.colonna))
        return ', '.join(f'{po.fila}{po.colonna}' for po in posti_ordinati)

    future, passate = [], []
    for p in prenotazioni:
        voce = {
            'prenotazione': p,
            'evento': p.evento,
            'sala': p.evento.sala,
            'posti_label': posti_label(p),
        }
        (future if p.evento.data_evento >= oggi else passate).append(voce)

    future.sort(key=lambda v: (v['evento'].data_evento, v['evento'].ora_inizio))
    passate.sort(key=lambda v: (v['evento'].data_evento, v['evento'].ora_inizio), reverse=True)

    return render_template('mie_prenotazioni.html', future=future, passate=passate)


# ==============================================================================
# 6. PRENOTAZIONI, GESTIONE POSTI E CHECK-IN
# ==============================================================================

@app.route('/booking/<int:event_id>')
@login_required
def booking_page(event_id):
    ev = db.session.get(Evento, event_id)
    if not ev:
        abort(404)
    if ev.data_evento < date.today():
        flash("Non e' possibile prenotare eventi passati.", 'danger')
        return redirect(url_for('calendar_view'))
    if ev.cancellato_google:
        flash("Questo evento è stato annullato (rimosso dal calendario Google di origine) e non è più prenotabile.", 'danger')
        return redirect(url_for('calendar_view'))
    return render_template('booking.html', evento=ev)


@app.route('/api/seats/<int:event_id>')
@login_required
def api_seats(event_id):
    evento = db.session.get(Evento, event_id)
    posti = Posto.query.options(
        joinedload(Posto.prenotazione).joinedload(Prenotazione.utente)
    ).filter_by(evento_id=event_id).order_by(Posto.fila, Posto.colonna).all()

    corridoio_colonne = []
    if evento and evento.corridoio_colonne:
        try:
            corridoio_colonne = [int(x.strip()) for x in evento.corridoio_colonne.split(',') if x.strip()]
        except ValueError:
            corridoio_colonne = []

    corridoio_file = []
    if evento and evento.corridoio_file:
        try:
            corridoio_file = [int(x.strip()) for x in evento.corridoio_file.split(',') if x.strip()]
        except ValueError:
            corridoio_file = []

    result = []
    for p in posti:
        item = {
            'id': p.id,
            'fila': p.fila,
            'colonna': p.colonna,
            'stato': p.stato,
            'numero_posto': p.numero_posto,
            'utente_id': None,
            'corridoio_colonne': corridoio_colonne,
            'corridoio_file': corridoio_file
        }
        if p.prenotazione:
            item['utente_id'] = p.prenotazione.utente_id
            item['is_mio'] = (p.prenotazione.utente_id == current_user.id)
            item['prenotazione_id'] = p.prenotazione.id
            item['nome_prenotazione'] = p.prenotazione.nome_prenotazione or p.prenotazione.utente.nome_cognome
            item['utente_nome'] = p.prenotazione.utente.nome_cognome
            if current_user.is_admin():
                item['utente'] = p.prenotazione.utente.nome_cognome
        else:
            item['is_mio'] = False
        result.append(item)
    return jsonify(result)


@app.route('/api/book', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_book():
    data = request.get_json(silent=True) or {}
    evento_id = data.get('evento_id')
    posti_ids = data.get('posti_ids', [])
    nome_prenotazione = data.get('nome_prenotazione', '').strip() or None

    if not evento_id or not posti_ids:
        return jsonify({'error': 'Dati mancanti'}), 400
    if not isinstance(posti_ids, list) or len(posti_ids) == 0:
        return jsonify({'error': 'Seleziona almeno un posto'}), 400

    try:
        evento = db.session.get(Evento, evento_id)
        if not evento:
            return jsonify({'error': 'Evento non trovato'}), 404
        if evento.data_evento < date.today():
            return jsonify({'error': 'Evento non prenotabile'}), 400

        posti = Posto.query.filter(
            Posto.id.in_(posti_ids),
            Posto.evento_id == evento_id,
            Posto.stato == 'libero'
        ).with_for_update().all()

        if len(posti) != len(posti_ids):
            db.session.rollback()
            return jsonify({'error': "Alcuni posti non sono piu' disponibili"}), 409

        prenotazione = Prenotazione(
            evento_id=evento_id,
            utente_id=current_user.id,
            nome_prenotazione=nome_prenotazione,
            stato='confermata'
        )
        db.session.add(prenotazione)
        db.session.flush()

        for p in posti:
            p.stato = 'prenotato'
            p.prenotazione_id = prenotazione.id

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Errore prenotazione: {e}')
        return jsonify({'error': 'Errore interno durante la prenotazione'}), 500

    run_email_task(
        app, _send_confirmation_email,
        evento.id, current_user.id,
        [p.id for p in posti],
        nome_prenotazione
    )
    return jsonify({'success': True, 'prenotazione_id': prenotazione.id})


@app.route('/api/reserve', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_reserve():
    if not current_user.is_admin():
        return jsonify({'error': 'Solo gli amministratori possono riservare posti'}), 403

    data = request.get_json(silent=True) or {}
    evento_id = data.get('evento_id')
    posti_ids = data.get('posti_ids', [])
    nome_prenotazione = data.get('nome_prenotazione', '').strip() or 'Riservato Admin'

    if not evento_id or not posti_ids:
        return jsonify({'error': 'Dati mancanti'}), 400

    try:
        posti = Posto.query.filter(
            Posto.id.in_(posti_ids),
            Posto.evento_id == evento_id,
            Posto.stato == 'libero'
        ).with_for_update().all()

        if len(posti) != len(posti_ids):
            db.session.rollback()
            return jsonify({'error': "Alcuni posti non sono piu' disponibili"}), 409

        prenotazione = Prenotazione(
            evento_id=evento_id,
            utente_id=current_user.id,
            nome_prenotazione=nome_prenotazione,
            stato='riservata'
        )
        db.session.add(prenotazione)
        db.session.flush()

        for p in posti:
            p.stato = 'riservato'
            p.prenotazione_id = prenotazione.id

        db.session.commit()
        return jsonify({'success': True, 'posti_riservati': len(posti), 'prenotazione_id': prenotazione.id})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Errore riserva: {e}')
        return jsonify({'error': 'Errore interno'}), 500


@app.route('/api/abbona', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_abbona():
    if not current_user.is_admin():
        return jsonify({'error': 'Solo gli amministratori possono abbonare posti'}), 403

    data = request.get_json(silent=True) or {}
    evento_id = data.get('evento_id')
    posti_ids = data.get('posti_ids', [])
    nome_prenotazione = data.get('nome_prenotazione', '').strip() or 'Abbonato'

    if not evento_id or not posti_ids:
        return jsonify({'error': 'Dati mancanti'}), 400

    try:
        posti = Posto.query.filter(
            Posto.id.in_(posti_ids),
            Posto.evento_id == evento_id,
            Posto.stato == 'libero'
        ).all()

        if len(posti) != len(posti_ids):
            db.session.rollback()
            return jsonify({'error': "Alcuni posti non sono piu' disponibili"}), 409

        prenotazione = Prenotazione(
            evento_id=evento_id,
            utente_id=current_user.id,
            nome_prenotazione=nome_prenotazione,
            stato='abbonata'
        )
        db.session.add(prenotazione)
        db.session.flush()

        for p in posti:
            p.stato = 'abbonato'
            p.prenotazione_id = prenotazione.id

        db.session.commit()
        return jsonify({'success': True, 'posti_abbonati': len(posti), 'prenotazione_id': prenotazione.id})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Errore abbonamento: {e}')
        return jsonify({'error': 'Errore interno'}), 500


@app.route('/api/delete-seats', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_delete_seats():
    data = request.get_json(silent=True) or {}
    posto_ids = data.get('posto_ids', [])

    if not posto_ids or not isinstance(posto_ids, list) or len(posto_ids) == 0:
        return jsonify({'error': 'Nessun posto selezionato'}), 400

    try:
        if current_user.is_admin():
            posti = Posto.query.filter(
                Posto.id.in_(posto_ids),
                Posto.stato.in_(['prenotato', 'riservato', 'abbonato'])
            ).with_for_update().all()
        else:
            posti = Posto.query.filter(
                Posto.id.in_(posto_ids),
                Posto.stato == 'prenotato'
            ).with_for_update().all()

        if len(posti) != len(posto_ids):
            db.session.rollback()
            return jsonify({'error': "Alcuni posti non sono piu' disponibili per la cancellazione"}), 409

        prenotazione_ids = list(set([p.prenotazione_id for p in posti if p.prenotazione_id]))

        prenotazioni = Prenotazione.query.options(
            joinedload(Prenotazione.utente),
            joinedload(Prenotazione.evento).joinedload(Evento.sala)
        ).filter(Prenotazione.id.in_(prenotazione_ids)).all()

        prenotazioni_dict = {p.id: p for p in prenotazioni}

        for p in posti:
            pren = prenotazioni_dict.get(p.prenotazione_id)
            if not pren:
                db.session.rollback()
                return jsonify({'error': 'Prenotazione non trovata per un posto'}), 404
            if not current_user.is_admin() and pren.utente_id != current_user.id:
                db.session.rollback()
                return jsonify({'error': 'Non puoi eliminare posti di un altro utente'}), 403

        prenotazioni_coinvolte = {}
        for p in posti:
            pid = p.prenotazione_id
            if pid not in prenotazioni_coinvolte:
                prenotazioni_coinvolte[pid] = {
                    'prenotazione': prenotazioni_dict[pid],
                    'posti': [],
                    'evento': prenotazioni_dict[pid].evento
                }
            prenotazioni_coinvolte[pid]['posti'].append(p)

        posti_str_parts = []
        for p in posti:
            posti_str_parts.append(f"{p.fila}{p.colonna}")
            p.stato = 'libero'
            p.prenotazione_id = None

        prenotazioni_da_eliminare = []
        for pid, info in prenotazioni_coinvolte.items():
            posti_rimanenti = Posto.query.filter_by(prenotazione_id=pid).count()
            if posti_rimanenti == 0:
                prenotazioni_da_eliminare.append(info['prenotazione'])

        for pren in prenotazioni_da_eliminare:
            db.session.delete(pren)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Errore eliminazione posti: {e}')
        return jsonify({'error': 'Errore interno'}), 500

    operatore = "Amministratore" if current_user.is_admin() else "Utente"
    operatore_nome = current_user.nome_cognome
    operatore_email = current_user.email

    email_data_list = []
    for pid, info in prenotazioni_coinvolte.items():
        pren = info['prenotazione']
        evento = info['evento']
        utente = pren.utente
        pren_esiste = db.session.get(Prenotazione, pid)
        prenotazione_eliminata = (pren_esiste is None)
        nome_pren = pren.nome_prenotazione
        posti_list = info['posti']
        posti_str_local = ', '.join([f"{p.fila}{p.colonna}" for p in posti_list])
        num_posti_local = len(posti_list)
        label = "posto" if num_posti_local == 1 else "posti"

        if prenotazione_eliminata:
            subject = f'Prenotazione Annullata - {num_posti_local} {label} - {evento.nome}'
            body = f"""Ciao {nome_pren or utente.nome_cognome},

La tua prenotazione per l'evento "{evento.nome}" e' stata annullata (tutti i posti rimossi).

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti annullati ({num_posti_local}): {posti_str_local}

Operazione effettuata da: {operatore} ({operatore_nome} - {operatore_email})

Se non hai richiesto tu questa operazione, contatta l'amministratore.
"""
        else:
            subject = f'Posti Annullati - {num_posti_local} {label} - {evento.nome}'
            body = f"""Ciao {nome_pren or utente.nome_cognome},

I posti {posti_str_local} per l'evento "{evento.nome}" sono stati annullati.

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti annullati ({num_posti_local}): {posti_str_local}

Operazione effettuata da: {operatore} ({operatore_nome} - {operatore_email})

Se non hai richiesto tu questa operazione, contatta l'amministratore.
"""

        email_data_list.append({
            'subject': subject,
            'recipient': utente.email,
            'body': body
        })

        if current_user.is_admin() and evento.sala.email_admin:
            admin_emails = [e.strip() for e in evento.sala.email_admin.split(',') if e.strip()]
            for admin_email in admin_emails:
                email_data_list.append({
                    'subject': f'Notifica: Posti Annullati da Admin - {evento.nome}',
                    'recipient': admin_email,
                    'body': f"""Notifica operazione di cancellazione:

Evento: {evento.nome}
Data: {evento.data_evento.strftime('%d/%m/%Y')}
Sala: {evento.sala.nome}
Posti annullati: {posti_str_local}

Prenotazione di: {utente.nome_cognome} ({utente.email})
Operazione effettuata da: {operatore_nome} ({operatore_email})

Questa e' una notifica automatica.
"""
                })

    run_email_task(app, _send_deletion_emails, email_data_list)

    return jsonify({
        'success': True,
        'posti_eliminati': len(posti),
        'prenotazioni_eliminate': len(prenotazioni_da_eliminare),
        'posti': posti_str_parts
    })


@app.route('/api/delete-booking', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_delete_booking():
    data = request.get_json(silent=True) or {}
    prenotazione_id = data.get('prenotazione_id')

    if not prenotazione_id:
        return jsonify({'error': 'ID prenotazione mancante'}), 400

    try:
        prenotazione = db.session.get(Prenotazione, prenotazione_id)
        if not prenotazione:
            return jsonify({'error': 'Prenotazione non trovata'}), 404

        if not current_user.is_admin() and prenotazione.utente_id != current_user.id:
            return jsonify({'error': 'Non puoi eliminare questa prenotazione'}), 403

        evento = prenotazione.evento
        utente = prenotazione.utente
        nome_pren = prenotazione.nome_prenotazione
        posti = Posto.query.filter_by(prenotazione_id=prenotazione_id).with_for_update().all()
        posti_str = ', '.join([f"{p.fila}{p.colonna}" for p in posti])

        for p in posti:
            p.stato = 'libero'
            p.prenotazione_id = None

        db.session.delete(prenotazione)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Errore eliminazione: {e}')
        return jsonify({'error': 'Errore interno'}), 500

    run_email_task(
        app, _send_cancellation_email,
        evento.id, utente.id, posti_str,
        True, nome_pren
    )
    return jsonify({'success': True})


@app.route('/admin/event/<int:event_id>')
@login_required
def admin_event_view(event_id):
    if not current_user.is_admin():
        flash('Accesso riservato.', 'danger')
        return redirect(url_for('calendar_view'))

    ev = db.session.get(Evento, event_id)
    if not ev:
        abort(404)

    search = request.args.get('search', '').strip()
    query = Prenotazione.query.filter_by(evento_id=event_id)
    if search:
        query = query.join(Utente).filter(
            or_(
                Utente.nome_cognome.ilike(f'%{search}%'),
                Utente.email.ilike(f'%{search}%'),
                Utente.username.ilike(f'%{search}%'),
                Prenotazione.nome_prenotazione.ilike(f'%{search}%')
            )
        )

    query = query.options(
        joinedload(Prenotazione.utente),
        joinedload(Prenotazione.posti)
    )

    prenotazioni = query.all()
    return render_template('admin_view.html', evento=ev, prenotazioni=prenotazioni, search=search)


@app.route('/api/prenotazione/<int:prenotazione_id>')
@login_required
def api_prenotazione_detail(prenotazione_id):
    if not current_user.is_admin():
        return jsonify({'error': 'Accesso negato'}), 403

    pren = Prenotazione.query.options(
        joinedload(Prenotazione.utente),
        joinedload(Prenotazione.posti)
    ).get_or_404(prenotazione_id)

    return jsonify({
        'id': pren.id,
        'utente': {
            'nome': pren.utente.nome_cognome,
            'email': pren.utente.email,
            'cellulare': pren.utente.cellulare,
            'username': pren.utente.username
        },
        'nome_prenotazione': pren.nome_prenotazione,
        'data_prenotazione': pren.data_prenotazione.strftime('%Y-%m-%d %H:%M'),
        'stato': pren.stato,
        'posti': [{'fila': p.fila, 'colonna': p.colonna, 'id': p.id} for p in pren.posti]
    })


@app.route('/api/prenotazione/<int:prenotazione_id>/presente', methods=['POST'])
@login_required
def api_prenotazione_toggle_presente(prenotazione_id):
    if not current_user.is_admin():
        return jsonify({'error': 'Accesso negato'}), 403

    pren = db.session.get(Prenotazione, prenotazione_id)
    if not pren:
        return jsonify({'error': 'Prenotazione non trovata'}), 404

    pren.presente = not pren.presente
    if pren.presente:
        pren.check_in_at = datetime.utcnow()
        pren.check_in_da = current_user.id
    else:
        pren.check_in_at = None
        pren.check_in_da = None
    db.session.commit()

    return jsonify({'success': True, 'presente': pren.presente})


@app.route('/api/prenotazione/<int:prenotazione_id>/rinomina', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def api_prenotazione_rinomina(prenotazione_id):
    data = request.get_json(silent=True) or {}
    nuovo_nome = (data.get('nome_prenotazione') or '').strip()
    applica_a_tutte = bool(data.get('applica_a_tutte'))
    posti_ids = data.get('posti_ids') or []
    if not isinstance(posti_ids, list):
        return jsonify({'error': 'Posti non validi'}), 400

    if not nuovo_nome:
        return jsonify({'error': 'Il nome non può essere vuoto'}), 400
    if len(nuovo_nome) > 150:
        return jsonify({'error': 'Nome troppo lungo'}), 400

    try:
        pren = db.session.get(Prenotazione, prenotazione_id)
        if not pren:
            return jsonify({'error': 'Prenotazione non trovata'}), 404

        is_admin = current_user.is_admin()
        if not is_admin:
            if pren.utente_id != current_user.id or pren.stato != 'confermata':
                return jsonify({'error': 'Non puoi modificare questa prenotazione'}), 403
            if pren.evento.data_evento < date.today():
                return jsonify({'error': 'Evento passato: modifica non consentita'}), 400

        vecchio_nome = pren.nome_prenotazione
        diviso = False

        if applica_a_tutte:
            # Spunta attiva: il nuovo nome vale per TUTTE le prenotazioni dell'evento
            # con lo stesso nome (e quindi per tutti i loro posti).
            da_aggiornare = [pren]
            q = Prenotazione.query.filter(
                Prenotazione.evento_id == pren.evento_id,
                Prenotazione.id != pren.id
            )
            if vecchio_nome:
                q = q.filter(func.lower(Prenotazione.nome_prenotazione) == vecchio_nome.lower())
            else:
                # nome vuoto: replica solo sulle prenotazioni senza nome dello stesso utente
                q = q.filter(
                    Prenotazione.nome_prenotazione.is_(None),
                    Prenotazione.utente_id == pren.utente_id
                )
            if not is_admin:
                q = q.filter(
                    Prenotazione.utente_id == current_user.id,
                    Prenotazione.stato == 'confermata'
                )
            da_aggiornare += q.with_for_update().all()
            for p in da_aggiornare:
                p.nome_prenotazione = nuovo_nome
        else:
            # Spunta non attiva: cambia SOLO il nome dei posti indicati. Il nome e' salvato
            # sulla prenotazione, quindi se i posti indicati sono solo una parte dei posti
            # della prenotazione, vengono staccati in una nuova prenotazione con il nuovo nome.
            try:
                richiesti = {int(x) for x in posti_ids}
            except (TypeError, ValueError):
                return jsonify({'error': 'Posti non validi'}), 400

            posti_pren = Posto.query.filter_by(prenotazione_id=pren.id).with_for_update().all()
            ids_pren = {p.id for p in posti_pren}

            if richiesti and not richiesti.issubset(ids_pren):
                return jsonify({'error': 'I posti indicati non appartengono a questa prenotazione'}), 400

            if richiesti and richiesti != ids_pren:
                nuova = Prenotazione(
                    evento_id=pren.evento_id,
                    utente_id=pren.utente_id,
                    nome_prenotazione=nuovo_nome,
                    stato=pren.stato,
                    data_prenotazione=pren.data_prenotazione,
                    presente=pren.presente,
                    check_in_at=pren.check_in_at,
                    check_in_da=pren.check_in_da
                )
                db.session.add(nuova)
                db.session.flush()
                for p in posti_pren:
                    if p.id in richiesti:
                        p.prenotazione_id = nuova.id
                da_aggiornare = [nuova]
                diviso = True
            else:
                pren.nome_prenotazione = nuovo_nome
                da_aggiornare = [pren]

        db.session.commit()
        return jsonify({
            'success': True,
            'nome_prenotazione': nuovo_nome,
            'prenotazioni_aggiornate': [p.id for p in da_aggiornare],
            'diviso': diviso
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Errore rinomina prenotazione: {e}')
        return jsonify({'error': 'Errore interno'}), 500


@app.route('/api/seats/search/<int:event_id>')
@login_required
def api_seats_search(event_id):
    if not current_user.is_admin():
        return jsonify({'error': 'Accesso negato'}), 403

    search = request.args.get('q', '').strip().lower()
    if not search:
        return jsonify({'error': 'Termine di ricerca richiesto'}), 400

    posti = Posto.query.options(
        joinedload(Posto.prenotazione).joinedload(Prenotazione.utente)
    ).filter_by(evento_id=event_id).all()

    matched = []
    for p in posti:
        if p.prenotazione:
            utente = p.prenotazione.utente
            nome_pren = p.prenotazione.nome_prenotazione or ''
            testo = f"{utente.nome_cognome} {utente.email} {utente.username} {nome_pren} {p.fila}{p.colonna}".lower()
            if search in testo:
                matched.append({
                    'id': p.id,
                    'fila': p.fila,
                    'colonna': p.colonna,
                    'stato': p.stato,
                    'utente': utente.nome_cognome,
                    'email': utente.email,
                    'nome_prenotazione': nome_pren
                })

    return jsonify({'matched': matched, 'count': len(matched), 'search': search})


# ==============================================================================
# 7. GESTIONE EVENTI E MODIFICA LAYOUT (ADMIN)
# ==============================================================================

def _ultima_fila_libera(evento):
    ultima_fila = chr(64 + evento.file)
    occupati = Posto.query.filter(
        Posto.evento_id == evento.id,
        Posto.fila == ultima_fila,
        Posto.stato != 'libero'
    ).count()
    return occupati == 0


def _ultima_colonna_libera(evento):
    occupati = Posto.query.filter(
        Posto.evento_id == evento.id,
        Posto.colonna == evento.colonne,
        Posto.stato != 'libero'
    ).count()
    return occupati == 0


@app.route('/event/create', methods=['GET', 'POST'])
@login_required
def create_event():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        descrizione = request.form.get('descrizione', '').strip()
        data_evento = request.form.get('data_evento')
        ora_inizio = request.form.get('ora_inizio')
        durata = request.form.get('durata', type=int)
        sala_id = request.form.get('sala_id', type=int)
        file = request.form.get('file', type=int)
        colonne = request.form.get('colonne', type=int)
        layout_id = request.form.get('layout_id', type=int) or None
        genere_evento_id = request.form.get('genere_evento_id', type=int) or None
        gestore_id = request.form.get('gestore_id', type=int) or None
        overbooking_abilitato = request.form.get('overbooking_abilitato') == 'on'
        salva_layout_nome = request.form.get('salva_layout_nome', '').strip()

        if not all([nome, data_evento, ora_inizio, durata, sala_id, file, colonne]):
            flash('Tutti i campi sono obbligatori.', 'danger')
            return redirect(url_for('create_event'))
        if file < 1 or file > 26 or colonne < 1:
            flash('File deve essere tra 1 e 26, colonne almeno 1.', 'danger')
            return redirect(url_for('create_event'))

        sala = db.session.get(Sala, sala_id)
        if not sala:
            flash('Sala non trovata.', 'danger')
            return redirect(url_for('create_event'))

        layout_scelto = None
        if layout_id:
            layout_scelto = db.session.get(LayoutPosti, layout_id)
            if not layout_scelto or layout_scelto.sala_id != sala_id:
                layout_scelto = None
                layout_id = None

        posti_max = file * colonne
        limite = sala.posti_max + (sala.overbooking_max if overbooking_abilitato else 0)

        if posti_max > limite:
            if overbooking_abilitato:
                flash(
                    f"I posti calcolati ({posti_max}) superano anche il limite di overbooking "
                    f"consentito per questa sala ({limite}).", 'danger'
                )
            else:
                flash(
                    f"I posti calcolati ({posti_max}) superano la capacita' della sala ({sala.posti_max}). "
                    f"Abilita l'overbooking se vuoi superarla (fino a {sala.posti_max + sala.overbooking_max}).",
                    'danger'
                )
            return redirect(url_for('create_event'))

        try:
            data_obj = datetime.strptime(data_evento, '%Y-%m-%d').date()
        except ValueError:
            flash('Formato data non valido.', 'danger')
            return redirect(url_for('create_event'))

        if data_obj < date.today():
            flash("Non e' possibile creare eventi nel passato.", 'danger')
            return redirect(url_for('create_event'))

        try:
            ora_obj = datetime.strptime(ora_inizio, '%H:%M').time()
        except ValueError:
            flash('Formato ora non valido.', 'danger')
            return redirect(url_for('create_event'))

        corridoio_colonne = request.form.get('corridoio_colonne', '').strip()
        corridoio_file = request.form.get('corridoio_file', '').strip()

        evento = Evento(
            nome=nome, descrizione=descrizione, data_evento=data_obj,
            ora_inizio=ora_obj, durata=durata, posti_max=posti_max,
            file=file, colonne=colonne,
            corridoio_colonne=corridoio_colonne, corridoio_file=corridoio_file,
            sala_id=sala_id, creato_da=current_user.id,
            layout_posti_id=layout_id, genere_evento_id=genere_evento_id,
            gestore_id=gestore_id,
            overbooking_abilitato=overbooking_abilitato
        )
        db.session.add(evento)
        db.session.flush()

        if not layout_id and salva_layout_nome:
            nuovo_layout = LayoutPosti(
                sala_id=sala_id,
                genere_evento_id=genere_evento_id,
                nome=salva_layout_nome,
                file=file, colonne=colonne,
                corridoio_colonne=corridoio_colonne, corridoio_file=corridoio_file,
                overbooking_abilitato=overbooking_abilitato,
                is_default=False,
                creato_da=current_user.id
            )
            db.session.add(nuovo_layout)
            db.session.flush()
            evento.layout_posti_id = nuovo_layout.id

        numero = 1
        posti_bulk = []
        for f in range(1, file + 1):
            fila_lettera = chr(64 + f)
            for c in range(1, colonne + 1):
                posti_bulk.append(Posto(
                    sala_id=sala_id, evento_id=evento.id, numero_posto=numero,
                    fila=fila_lettera, colonna=c, stato='libero'
                ))
                numero += 1

        db.session.add_all(posti_bulk)
        db.session.commit()
        flash('Evento creato con successo!', 'success')
        return redirect(url_for('calendar_view'))

    sale = Sala.query.order_by(Sala.nome).all()
    generi = GenereEvento.query.order_by(GenereEvento.nome).all()
    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    layouts = LayoutPosti.query.all()
    real_today = date.today().strftime('%Y-%m-%d')
    selected_date = request.args.get('date', '')
    return render_template(
        'event_create.html', sale=sale, generi=generi, gestori=gestori, layouts=layouts,
        today=real_today, selected_date=selected_date
    )


@app.route('/admin/event/<int:event_id>/layout')
@login_required
def admin_evento_layout(event_id):
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)
    sala = evento.sala

    return render_template(
        'admin_evento_layout.html',
        evento=evento, sala=sala,
        ultima_fila_libera=_ultima_fila_libera(evento) if evento.file > 1 else False,
        ultima_colonna_libera=_ultima_colonna_libera(evento) if evento.colonne > 1 else False
    )


@app.route('/admin/event/<int:event_id>/layout/aggiungi-file', methods=['POST'])
@login_required
def admin_evento_aggiungi_file(event_id):
    if not current_user.is_admin():
        abort(403)

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)
    sala = evento.sala

    n = request.form.get('numero', type=int) or 1
    if n < 1:
        flash('Numero di file da aggiungere non valido.', 'danger')
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    overbooking_abilitato = request.form.get('overbooking_abilitato') == 'on'
    nuovo_file = evento.file + n
    nuovo_posti_max = nuovo_file * evento.colonne
    limite = sala.posti_max + (sala.overbooking_max if overbooking_abilitato else 0)

    if nuovo_posti_max > limite:
        flash(
            f"Aggiungendo {n} file arriveresti a {nuovo_posti_max} posti, oltre il limite consentito "
            f"({limite}{' con overbooking' if overbooking_abilitato else ''}).", 'danger'
        )
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    if nuovo_file > 26:
        flash('Non è possibile superare 26 file (lettere A-Z).', 'danger')
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    ultimo_numero = db.session.query(func.max(Posto.numero_posto)).filter_by(evento_id=event_id).scalar() or 0
    nuovi_posti = []
    for f in range(evento.file + 1, nuovo_file + 1):
        fila_lettera = chr(64 + f)
        for c in range(1, evento.colonne + 1):
            ultimo_numero += 1
            nuovi_posti.append(Posto(
                sala_id=evento.sala_id, evento_id=event_id, numero_posto=ultimo_numero,
                fila=fila_lettera, colonna=c, stato='libero'
            ))

    evento.file = nuovo_file
    evento.posti_max = nuovo_posti_max
    if overbooking_abilitato:
        evento.overbooking_abilitato = True

    db.session.add_all(nuovi_posti)
    db.session.commit()
    flash(f"Aggiunte {n} file ({len(nuovi_posti)} nuovi posti).", 'success')
    return redirect(url_for('admin_evento_layout', event_id=event_id))


@app.route('/admin/event/<int:event_id>/layout/aggiungi-colonne', methods=['POST'])
@login_required
def admin_evento_aggiungi_colonne(event_id):
    if not current_user.is_admin():
        abort(403)

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)
    sala = evento.sala

    n = request.form.get('numero', type=int) or 1
    if n < 1:
        flash('Numero di colonne da aggiungere non valido.', 'danger')
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    overbooking_abilitato = request.form.get('overbooking_abilitato') == 'on'
    nuove_colonne = evento.colonne + n
    nuovo_posti_max = evento.file * nuove_colonne
    limite = sala.posti_max + (sala.overbooking_max if overbooking_abilitato else 0)

    if nuovo_posti_max > limite:
        flash(
            f"Aggiungendo {n} colonne arriveresti a {nuovo_posti_max} posti, oltre il limite consentito "
            f"({limite}{' con overbooking' if overbooking_abilitato else ''}).", 'danger'
        )
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    ultimo_numero = db.session.query(func.max(Posto.numero_posto)).filter_by(evento_id=event_id).scalar() or 0
    nuovi_posti = []
    for f in range(1, evento.file + 1):
        fila_lettera = chr(64 + f)
        for c in range(evento.colonne + 1, nuove_colonne + 1):
            ultimo_numero += 1
            nuovi_posti.append(Posto(
                sala_id=evento.sala_id, evento_id=event_id, numero_posto=ultimo_numero,
                fila=fila_lettera, colonna=c, stato='libero'
            ))

    evento.colonne = nuove_colonne
    evento.posti_max = nuovo_posti_max
    if overbooking_abilitato:
        evento.overbooking_abilitato = True

    db.session.add_all(nuovi_posti)
    db.session.commit()
    flash(f"Aggiunte {n} colonne ({len(nuovi_posti)} nuovi posti).", 'success')
    return redirect(url_for('admin_evento_layout', event_id=event_id))


@app.route('/admin/event/<int:event_id>/layout/rimuovi-file', methods=['POST'])
@login_required
def admin_evento_rimuovi_file(event_id):
    if not current_user.is_admin():
        abort(403)

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)

    if evento.file <= 1:
        flash("Non puoi rimuovere l'unica fila rimasta.", 'danger')
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    ultima_fila = chr(64 + evento.file)

    try:
        posti_ultima_fila = Posto.query.filter(
            Posto.evento_id == event_id,
            Posto.fila == ultima_fila
        ).with_for_update().all()

        occupati = [p for p in posti_ultima_fila if p.stato != 'libero']
        if occupati:
            db.session.rollback()
            flash(
                f"Impossibile rimuovere l'ultima fila ({ultima_fila}): {len(occupati)} posti non sono liberi. "
                f"Libera o sposta prima quelle prenotazioni.", 'danger'
            )
            return redirect(url_for('admin_evento_layout', event_id=event_id))

        for p in posti_ultima_fila:
            db.session.delete(p)

        evento.file -= 1
        evento.posti_max = evento.file * evento.colonne
        db.session.commit()
        flash(f"Fila {ultima_fila} rimossa ({len(posti_ultima_fila)} posti eliminati).", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Errore durante la rimozione: {str(e)}", 'danger')

    return redirect(url_for('admin_evento_layout', event_id=event_id))


@app.route('/admin/event/<int:event_id>/layout/rimuovi-colonna', methods=['POST'])
@login_required
def admin_evento_rimuovi_colonna(event_id):
    if not current_user.is_admin():
        abort(403)

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)

    if evento.colonne <= 1:
        flash("Non puoi rimuovere l'unica colonna rimasta.", 'danger')
        return redirect(url_for('admin_evento_layout', event_id=event_id))

    ultima_colonna = evento.colonne

    try:
        posti_ultima_colonna = Posto.query.filter(
            Posto.evento_id == event_id,
            Posto.colonna == ultima_colonna
        ).with_for_update().all()

        occupati = [p for p in posti_ultima_colonna if p.stato != 'libero']
        if occupati:
            db.session.rollback()
            flash(
                f"Impossibile rimuovere l'ultima colonna ({ultima_colonna}): {len(occupati)} posti non sono liberi. "
                f"Libera o sposta prima quelle prenotazioni.", 'danger'
            )
            return redirect(url_for('admin_evento_layout', event_id=event_id))

        for p in posti_ultima_colonna:
            db.session.delete(p)

        evento.colonne -= 1
        evento.posti_max = evento.file * evento.colonne
        db.session.commit()
        flash(f"Colonna {ultima_colonna} rimossa ({len(posti_ultima_colonna)} posti eliminati).", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Errore durante la rimozione: {str(e)}", 'danger')

    return redirect(url_for('admin_evento_layout', event_id=event_id))


@app.route('/admin/event/<int:event_id>/layout/corridoi', methods=['POST'])
@login_required
def admin_evento_corridoi(event_id):
    if not current_user.is_admin():
        abort(403)

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)

    evento.corridoio_colonne = request.form.get('corridoio_colonne', '').strip()
    evento.corridoio_file = request.form.get('corridoio_file', '').strip()
    db.session.commit()
    flash('Corridoi aggiornati.', 'success')
    return redirect(url_for('admin_evento_layout', event_id=event_id))


@app.route('/api/event/delete/<int:event_id>', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def api_delete_event(event_id):
    if not current_user.is_admin():
        return jsonify({'error': 'Solo gli amministratori possono eliminare eventi'}), 403

    try:
        evento = db.session.get(Evento, event_id)
        if not evento:
            return jsonify({'error': 'Evento non trovato'}), 404

        prenotazioni = Prenotazione.query.filter_by(evento_id=event_id).all()
        for p in prenotazioni:
            posti = Posto.query.filter_by(prenotazione_id=p.id).all()
            for posto in posti:
                posto.stato = 'libero'
                posto.prenotazione_id = None
            db.session.delete(p)

        posti_evento = Posto.query.filter_by(evento_id=event_id).all()
        for posto in posti_evento:
            db.session.delete(posto)

        db.session.delete(evento)
        db.session.commit()

        return jsonify({'success': True, 'message': 'Evento eliminato con successo'})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Errore eliminazione evento: {e}")
        return jsonify({"error": "Errore interno durante l'eliminazione"}), 500


# ==============================================================================
# 8. GESTIONE GENERI, GESTORI, SALE E LAYOUT SALVATI (ADMIN)
# ==============================================================================

LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
LOGO_MIMETYPES_AMMESSI = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'}


def _salva_logo_da_form(oggetto, campo_file='logo', campo_rimuovi='rimuovi_logo'):
    if request.form.get(campo_rimuovi) == 'on':
        oggetto.logo = None
        oggetto.logo_mimetype = None
        return None

    file = request.files.get(campo_file)
    if file and file.filename:
        dati = file.read()
        if len(dati) > LOGO_MAX_BYTES:
            return f'Il file "{file.filename}" supera i {LOGO_MAX_BYTES // (1024*1024)} MB consentiti.'
        mimetype = file.mimetype or ''
        if mimetype not in LOGO_MIMETYPES_AMMESSI:
            return f'Formato "{mimetype}" non supportato. Usa PNG, JPG, GIF, WEBP o SVG.'
        oggetto.logo = dati
        oggetto.logo_mimetype = mimetype
    return None


@app.route('/gestore/<int:gestore_id>/logo')
def logo_gestore(gestore_id):
    gestore = db.session.get(Gestore, gestore_id)
    if not gestore or not gestore.logo:
        abort(404)
    return Response(gestore.logo, mimetype=gestore.logo_mimetype or 'application/octet-stream')


@app.route('/genere/<int:genere_id>/logo')
def logo_genere(genere_id):
    genere = db.session.get(GenereEvento, genere_id)
    if not genere or not genere.logo:
        abort(404)
    return Response(genere.logo, mimetype=genere.logo_mimetype or 'application/octet-stream')


@app.route('/admin/gestori')
@login_required
def admin_gestori():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    modifica_id = request.args.get('modifica', type=int)
    gestore_da_modificare = db.session.get(Gestore, modifica_id) if modifica_id else None

    return render_template('admin_gestori.html', gestori=gestori, gestore_da_modificare=gestore_da_modificare)


@app.route('/admin/gestori/add', methods=['POST'])
@login_required
def admin_gestori_add():
    if not current_user.is_admin():
        abort(403)

    ragione_sociale = (request.form.get('ragione_sociale') or '').strip()
    if not ragione_sociale:
        flash('La ragione sociale è obbligatoria.', 'danger')
        return redirect(url_for('admin_gestori'))

    gestore = Gestore(
        ragione_sociale=ragione_sociale,
        indirizzo=(request.form.get('indirizzo') or '').strip() or None,
        cf_piva=(request.form.get('cf_piva') or '').strip() or None,
        cellulare=(request.form.get('cellulare') or '').strip() or None,
        email=(request.form.get('email') or '').strip() or None,
        pec=(request.form.get('pec') or '').strip() or None,
        certificazioni=(request.form.get('certificazioni') or '').strip() or None,
        creato_da=current_user.id,
    )

    errore = _salva_logo_da_form(gestore)
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_gestori'))

    db.session.add(gestore)
    db.session.commit()
    flash(f'Gestore "{gestore.ragione_sociale}" creato.', 'success')
    return redirect(url_for('admin_gestori'))


@app.route('/admin/gestori/<int:gestore_id>/edit', methods=['POST'])
@login_required
def admin_gestori_edit(gestore_id):
    if not current_user.is_admin():
        abort(403)

    gestore = db.session.get(Gestore, gestore_id)
    if not gestore:
        abort(404)

    ragione_sociale = (request.form.get('ragione_sociale') or '').strip()
    if not ragione_sociale:
        flash('La ragione sociale è obbligatoria.', 'danger')
        return redirect(url_for('admin_gestori', modifica=gestore_id))

    gestore.ragione_sociale = ragione_sociale
    gestore.indirizzo = (request.form.get('indirizzo') or '').strip() or None
    gestore.cf_piva = (request.form.get('cf_piva') or '').strip() or None
    gestore.cellulare = (request.form.get('cellulare') or '').strip() or None
    gestore.email = (request.form.get('email') or '').strip() or None
    gestore.pec = (request.form.get('pec') or '').strip() or None
    gestore.certificazioni = (request.form.get('certificazioni') or '').strip() or None

    errore = _salva_logo_da_form(gestore)
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_gestori', modifica=gestore_id))

    db.session.commit()
    flash(f'Gestore "{gestore.ragione_sociale}" aggiornato.', 'success')
    return redirect(url_for('admin_gestori'))


@app.route('/admin/gestori/<int:gestore_id>/delete', methods=['POST'])
@login_required
def admin_gestori_delete(gestore_id):
    if not current_user.is_admin():
        abort(403)

    gestore = db.session.get(Gestore, gestore_id)
    if not gestore:
        abort(404)

    stagioni_collegate = Stagione.query.filter_by(gestore_id=gestore_id).count()
    if stagioni_collegate > 0:
        flash(
            f"Impossibile eliminare '{gestore.ragione_sociale}': ha {stagioni_collegate} stagioni collegate. "
            f"Elimina prima le stagioni.",
            'danger'
        )
        return redirect(url_for('admin_gestori'))

    ripetitive_collegate = PrenotazioneRipetitiva.query.filter_by(gestore_id=gestore_id).count()
    if ripetitive_collegate > 0:
        flash(
            f"Impossibile eliminare '{gestore.ragione_sociale}': ha {ripetitive_collegate} posti di abbonati collegati. "
            f"Eliminali prima da Gestione Abbonati.",
            'danger'
        )
        return redirect(url_for('admin_gestori'))

    db.session.delete(gestore)
    db.session.commit()
    flash('Gestore eliminato.', 'success')
    return redirect(url_for('admin_gestori'))


# ------------------------------------------------------------------------------
# GESTIONE STAGIONE - Fase 1: anagrafica delle stagioni
# ------------------------------------------------------------------------------

def _leggi_stagione_da_form(escludi_id=None):
    """Legge e valida il form di una Stagione. Ritorna (dati, errore): uno dei due e' None."""
    nome = (request.form.get('nome_stagione') or '').strip()
    descrizione = (request.form.get('descrizione') or '').strip() or None
    gestore_id = request.form.get('gestore_id', type=int)

    if not nome:
        return None, 'Il nome della stagione è obbligatorio.'
    if len(nome) > 150:
        return None, 'Il nome della stagione è troppo lungo (massimo 150 caratteri).'
    if not gestore_id or not db.session.get(Gestore, gestore_id):
        return None, 'Seleziona un gestore valido.'

    try:
        data_inizio = datetime.strptime(request.form.get('data_inizio') or '', '%Y-%m-%d').date()
        data_fine = datetime.strptime(request.form.get('data_fine') or '', '%Y-%m-%d').date()
    except ValueError:
        return None, 'Le date di inizio e fine sono obbligatorie e devono essere valide.'
    if data_fine < data_inizio:
        return None, 'La data di fine non può precedere la data di inizio.'

    duplicata = Stagione.query.filter(
        func.lower(Stagione.nome_stagione) == nome.lower(),
        Stagione.gestore_id == gestore_id
    )
    if escludi_id:
        duplicata = duplicata.filter(Stagione.id != escludi_id)
    if duplicata.first():
        return None, f"Esiste già una stagione chiamata '{nome}' per questo gestore."

    return {
        'nome_stagione': nome, 'descrizione': descrizione, 'gestore_id': gestore_id,
        'data_inizio': data_inizio, 'data_fine': data_fine,
    }, None


@app.route('/admin/stagioni')
@login_required
def admin_stagioni():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    filtro_gestore_id = request.args.get('gestore_id', type=int)

    query = Stagione.query.options(joinedload(Stagione.gestore))
    if filtro_gestore_id:
        query = query.filter(Stagione.gestore_id == filtro_gestore_id)
    stagioni = query.order_by(Stagione.data_inizio.desc(), Stagione.nome_stagione).all()

    # eventi del gestore che cadono nel periodo della stagione (solo informativo)
    conteggi_eventi = {
        s.id: Evento.query.filter(
            Evento.gestore_id == s.gestore_id,
            Evento.data_evento >= s.data_inizio,
            Evento.data_evento <= s.data_fine
        ).count()
        for s in stagioni
    }

    modifica_id = request.args.get('modifica', type=int)
    stagione_da_modificare = db.session.get(Stagione, modifica_id) if modifica_id else None

    return render_template(
        'admin_stagioni.html', stagioni=stagioni, gestori=gestori,
        filtro_gestore_id=filtro_gestore_id, conteggi_eventi=conteggi_eventi,
        stagione_da_modificare=stagione_da_modificare, oggi=date.today()
    )


@app.route('/admin/stagioni/add', methods=['POST'])
@login_required
def admin_stagioni_add():
    if not current_user.is_admin():
        abort(403)

    dati, errore = _leggi_stagione_da_form()
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_stagioni'))

    stagione = Stagione(creato_da=current_user.id, **dati)
    db.session.add(stagione)
    db.session.commit()
    flash(f"Stagione '{stagione.nome_stagione}' creata.", 'success')
    return redirect(url_for('admin_stagioni', gestore_id=stagione.gestore_id))


@app.route('/admin/stagioni/<int:stagione_id>/edit', methods=['POST'])
@login_required
def admin_stagioni_edit(stagione_id):
    if not current_user.is_admin():
        abort(403)

    stagione = db.session.get(Stagione, stagione_id)
    if not stagione:
        abort(404)

    dati, errore = _leggi_stagione_da_form(escludi_id=stagione_id)
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_stagioni', modifica=stagione_id))

    for campo, valore in dati.items():
        setattr(stagione, campo, valore)
    db.session.commit()
    flash(f"Stagione '{stagione.nome_stagione}' aggiornata.", 'success')
    return redirect(url_for('admin_stagioni', gestore_id=stagione.gestore_id))


@app.route('/admin/stagioni/<int:stagione_id>/delete', methods=['POST'])
@login_required
def admin_stagioni_delete(stagione_id):
    if not current_user.is_admin():
        abort(403)

    stagione = db.session.get(Stagione, stagione_id)
    if not stagione:
        abort(404)

    ripetitive_collegate = PrenotazioneRipetitiva.query.filter_by(stagione_id=stagione_id).count()
    if ripetitive_collegate > 0:
        flash(
            f"Impossibile eliminare '{stagione.nome_stagione}': ha {ripetitive_collegate} posti di abbonati collegati. "
            f"Eliminali prima da Gestione Abbonati.",
            'danger'
        )
        return redirect(url_for('admin_stagioni'))

    nome = stagione.nome_stagione
    db.session.delete(stagione)
    db.session.commit()
    flash(f"Stagione '{nome}' eliminata.", 'success')
    return redirect(url_for('admin_stagioni'))


# ------------------------------------------------------------------------------
# GESTIONE ABBONATI - Fase 2: categorie e prenotazioni ripetitive
# ------------------------------------------------------------------------------

CATEGORIE_RIPETITIVE_INIZIALI = [
    # nome, colore, stato_posto, durata, promozione_automatica, soglia, priorita (piu' alta vince)
    ('Abbonato', '#8b5cf6', 'abbonato', 'stagione', False, None, 30),
    ('Fedelissimo', '#eab308', 'abbonato', 'permanente', True, 3, 20),
    ('Vip', '#ec4899', 'riservato', 'stagione', False, None, 10),
]
_RE_COLORE = re.compile(r'^#[0-9a-fA-F]{6}$')
_RE_POSTO = re.compile(r'^([A-Z])(\d{1,3})$')


def _seed_categorie_ripetitive():
    """Inserisce le categorie iniziali (modificabili dall'admin) se la tabella e' vuota."""
    if CategoriaRipetitiva.query.count() > 0:
        return
    for nome, colore, stato, durata, auto, soglia, priorita in CATEGORIE_RIPETITIVE_INIZIALI:
        db.session.add(CategoriaRipetitiva(
            nome=nome, colore=colore, stato_posto=stato, durata=durata,
            promozione_automatica=auto, soglia_partecipazioni=soglia, priorita=priorita, attiva=True
        ))
    db.session.commit()
    app.logger.info('startup-migration: inserite le categorie ripetitive iniziali')


def _normalizza_persona(nome):
    """Chiave per riconoscere la stessa persona: senza maiuscole, accenti, punteggiatura e spazi doppi.
    "CHILO' Gabriele" e "Chilo Gabriele" danno la stessa chiave."""
    testo = unicodedata.normalize('NFKD', nome or '')
    testo = ''.join(ch for ch in testo if not unicodedata.combining(ch))
    testo = re.sub(r'[^\w\s]', '', testo.casefold())
    return ' '.join(testo.split())


def _layout_per_sala_genere(sala_id, genere_id):
    """Layout di riferimento per scegliere i posti: quello predefinito del genere, altrimenti quello della sala."""
    layout = LayoutPosti.query.filter_by(sala_id=sala_id, genere_evento_id=genere_id, is_default=True).first()
    return layout or _layout_default_per_sala(sala_id)


def _filtro_stagione(stagione_id):
    if stagione_id is None:
        return PrenotazioneRipetitiva.stagione_id.is_(None)
    return PrenotazioneRipetitiva.stagione_id == stagione_id


def _righe_gruppo(riga):
    """Tutte le righe (un posto ciascuna) della stessa persona/categoria/stagione/genere/sala/gestore."""
    return PrenotazioneRipetitiva.query.filter(
        PrenotazioneRipetitiva.gestore_id == riga.gestore_id,
        PrenotazioneRipetitiva.genere_evento_id == riga.genere_evento_id,
        PrenotazioneRipetitiva.sala_id == riga.sala_id,
        PrenotazioneRipetitiva.categoria_id == riga.categoria_id,
        PrenotazioneRipetitiva.persona_chiave == riga.persona_chiave,
        _filtro_stagione(riga.stagione_id)
    ).order_by(PrenotazioneRipetitiva.fila, PrenotazioneRipetitiva.colonna).all()


def _elimina_ripetitive(righe):
    """Elimina le righe e pulisce i riferimenti (posti degli eventi gia' caricati ed esclusioni)."""
    ids = [r.id for r in righe]
    if not ids:
        return
    Posto.query.filter(Posto.ripetitiva_id.in_(ids)).update({Posto.ripetitiva_id: None}, synchronize_session=False)
    EsclusioneRipetitiva.query.filter(EsclusioneRipetitiva.ripetitiva_id.in_(ids)).delete(synchronize_session=False)
    for r in righe:
        db.session.delete(r)


def _ordina_codici_posto(codici):
    """Ordine naturale: A1, A2, A10, B1 (e non A1, A10, A2)."""
    def chiave(c):
        m = _RE_POSTO.match(c)
        return (m.group(1), int(m.group(2))) if m else (c, 0)
    return sorted(codici, key=chiave)


# ---- Categorie ----

def _leggi_categoria_da_form(categoria_esistente=None):
    """Legge e valida il form di una Categoria. Ritorna (dati, errore)."""
    nome = (request.form.get('nome') or '').strip()
    colore = (request.form.get('colore') or '').strip()
    stato_posto = request.form.get('stato_posto')
    durata = request.form.get('durata')
    promozione = request.form.get('promozione_automatica') == 'on'
    soglia = request.form.get('soglia_partecipazioni', type=int)
    priorita = request.form.get('priorita', type=int)
    attiva = request.form.get('attiva') == 'on'

    if not nome:
        return None, 'Il nome della categoria è obbligatorio.'
    if len(nome) > 50:
        return None, 'Il nome della categoria è troppo lungo (massimo 50 caratteri).'
    if not _RE_COLORE.match(colore):
        return None, 'Il colore non è valido.'
    if stato_posto not in ('abbonato', 'riservato'):
        return None, 'Scegli come deve risultare il posto in mappa (abbonato o riservato).'
    if durata not in ('stagione', 'permanente'):
        return None, 'Scegli la durata della categoria.'
    if promozione and durata != 'permanente':
        return None, 'La promozione automatica è disponibile solo per le categorie permanenti.'
    if promozione and (soglia is None or soglia < 1):
        return None, 'Indica dopo quante partecipazioni scatta la promozione automatica (almeno 1).'
    if priorita is None:
        priorita = 0
    if not 0 <= priorita <= 999:
        return None, 'La priorità deve essere un numero tra 0 e 999.'

    duplicata = CategoriaRipetitiva.query.filter(func.lower(CategoriaRipetitiva.nome) == nome.lower())
    if categoria_esistente:
        duplicata = duplicata.filter(CategoriaRipetitiva.id != categoria_esistente.id)
    if duplicata.first():
        return None, f"Esiste già una categoria chiamata '{nome}'."

    if categoria_esistente and categoria_esistente.durata != durata:
        if PrenotazioneRipetitiva.query.filter_by(categoria_id=categoria_esistente.id).count() > 0:
            return None, ("Non puoi cambiare la durata di una categoria già usata da abbonati: "
                          "crea una nuova categoria oppure elimina prima quegli abbonati.")

    return {
        'nome': nome, 'colore': colore.lower(), 'stato_posto': stato_posto, 'durata': durata,
        'promozione_automatica': promozione, 'soglia_partecipazioni': soglia if promozione else None,
        'priorita': priorita, 'attiva': attiva,
    }, None


@app.route('/admin/categorie-abbonati')
@login_required
def admin_categorie_abbonati():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    categorie = CategoriaRipetitiva.query.order_by(CategoriaRipetitiva.priorita.desc(), CategoriaRipetitiva.nome).all()
    utilizzi = dict(
        db.session.query(PrenotazioneRipetitiva.categoria_id, func.count(PrenotazioneRipetitiva.id))
        .group_by(PrenotazioneRipetitiva.categoria_id).all()
    )
    modifica_id = request.args.get('modifica', type=int)
    categoria_da_modificare = db.session.get(CategoriaRipetitiva, modifica_id) if modifica_id else None
    return render_template(
        'admin_categorie_abbonati.html', categorie=categorie, utilizzi=utilizzi,
        categoria_da_modificare=categoria_da_modificare
    )


@app.route('/admin/categorie-abbonati/add', methods=['POST'])
@login_required
def admin_categorie_abbonati_add():
    if not current_user.is_admin():
        abort(403)
    dati, errore = _leggi_categoria_da_form()
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_categorie_abbonati'))
    db.session.add(CategoriaRipetitiva(**dati))
    db.session.commit()
    flash(f"Categoria '{dati['nome']}' creata.", 'success')
    return redirect(url_for('admin_categorie_abbonati'))


@app.route('/admin/categorie-abbonati/<int:categoria_id>/edit', methods=['POST'])
@login_required
def admin_categorie_abbonati_edit(categoria_id):
    if not current_user.is_admin():
        abort(403)
    categoria = db.session.get(CategoriaRipetitiva, categoria_id)
    if not categoria:
        abort(404)
    dati, errore = _leggi_categoria_da_form(categoria_esistente=categoria)
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_categorie_abbonati', modifica=categoria_id))
    for campo, valore in dati.items():
        setattr(categoria, campo, valore)
    db.session.commit()
    flash(f"Categoria '{categoria.nome}' aggiornata.", 'success')
    return redirect(url_for('admin_categorie_abbonati'))


@app.route('/admin/categorie-abbonati/<int:categoria_id>/delete', methods=['POST'])
@login_required
def admin_categorie_abbonati_delete(categoria_id):
    if not current_user.is_admin():
        abort(403)
    categoria = db.session.get(CategoriaRipetitiva, categoria_id)
    if not categoria:
        abort(404)
    usi = PrenotazioneRipetitiva.query.filter_by(categoria_id=categoria_id).count()
    if usi > 0:
        flash(f"Impossibile eliminare '{categoria.nome}': è usata da {usi} posti di abbonati. "
              f"Puoi disattivarla dalla modifica.", 'danger')
        return redirect(url_for('admin_categorie_abbonati'))
    nome = categoria.nome
    db.session.delete(categoria)
    db.session.commit()
    flash(f"Categoria '{nome}' eliminata.", 'success')
    return redirect(url_for('admin_categorie_abbonati'))


# ---- Abbonati (prenotazioni ripetitive) ----

def _leggi_ripetitiva_da_form(categoria_corrente_id=None):
    """Valida il modulo Gestione Abbonati. Ritorna (dati, errore)."""
    nome = (request.form.get('nome_prenotazione') or '').strip()
    gestore_id = request.form.get('gestore_id', type=int)
    categoria_id = request.form.get('categoria_id', type=int)
    stagione_id = request.form.get('stagione_id', type=int)
    genere_id = request.form.get('genere_id', type=int)
    sala_id = request.form.get('sala_id', type=int)
    codici = [c.strip().upper() for c in (request.form.get('posti') or '').split(',') if c.strip()]

    if not nome:
        return None, 'Il nome della prenotazione è obbligatorio.'
    if len(nome) > 150:
        return None, 'Il nome della prenotazione è troppo lungo (massimo 150 caratteri).'
    if not _normalizza_persona(nome):
        return None, 'Il nome della prenotazione deve contenere almeno una lettera o un numero.'

    gestore = db.session.get(Gestore, gestore_id) if gestore_id else None
    if not gestore:
        return None, 'Seleziona un gestore valido.'

    categoria = db.session.get(CategoriaRipetitiva, categoria_id) if categoria_id else None
    if not categoria:
        return None, 'Seleziona una categoria valida.'
    if not categoria.attiva and categoria.id != categoria_corrente_id:
        return None, f"La categoria '{categoria.nome}' non è attiva."

    if categoria.durata == 'stagione':
        stagione = db.session.get(Stagione, stagione_id) if stagione_id else None
        if not stagione or stagione.gestore_id != gestore.id:
            return None, 'Per questa categoria serve una stagione del gestore selezionato.'
    else:
        stagione_id = None  # categoria permanente: vale anche nelle stagioni successive

    genere = db.session.get(GenereEvento, genere_id) if genere_id else None
    if not genere or genere.gestore_id not in (None, gestore.id):
        return None, 'Seleziona un genere del gestore (o un genere senza gestore).'

    sala = db.session.get(Sala, sala_id) if sala_id else None
    if not sala:
        return None, 'Seleziona una sala valida.'

    layout = _layout_per_sala_genere(sala.id, genere.id)
    if not layout:
        return None, f"Per la sala '{sala.nome}' non è configurato un layout predefinito: impossibile scegliere i posti."

    if not codici:
        return None, 'Seleziona almeno un posto sulla mappa.'
    posti = []
    for codice in codici:
        m = _RE_POSTO.match(codice)
        if not m:
            return None, f"Posto non valido: '{codice}'."
        fila, colonna = m.group(1), int(m.group(2))
        if not 1 <= ord(fila) - 64 <= layout.file or not 1 <= colonna <= layout.colonne:
            return None, f"Il posto {codice} non esiste nel layout '{layout.nome}' ({layout.file} file x {layout.colonne} colonne)."
        if (fila, colonna) not in posti:
            posti.append((fila, colonna))

    return {
        'nome': nome, 'persona_chiave': _normalizza_persona(nome), 'gestore_id': gestore.id,
        'categoria_id': categoria.id, 'stagione_id': stagione_id, 'genere_id': genere.id,
        'sala_id': sala.id, 'posti': posti,
    }, None


@app.route('/admin/abbonati')
@login_required
def admin_abbonati():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    stagioni = Stagione.query.order_by(Stagione.data_inizio.desc(), Stagione.nome_stagione).all()
    generi = GenereEvento.query.order_by(GenereEvento.nome).all()
    sale = Sala.query.order_by(Sala.nome).all()
    categorie = CategoriaRipetitiva.query.order_by(CategoriaRipetitiva.priorita.desc(), CategoriaRipetitiva.nome).all()

    filtri = {
        'gestore_id': request.args.get('gestore_id', type=int),
        'stagione_id': request.args.get('stagione_id', type=int),
        'genere_id': request.args.get('genere_id', type=int),
        'sala_id': request.args.get('sala_id', type=int),
        'categoria_id': request.args.get('categoria_id', type=int),
        'q': (request.args.get('q') or '').strip(),
    }
    query = PrenotazioneRipetitiva.query.options(
        joinedload(PrenotazioneRipetitiva.categoria), joinedload(PrenotazioneRipetitiva.gestore),
        joinedload(PrenotazioneRipetitiva.stagione), joinedload(PrenotazioneRipetitiva.genere),
        joinedload(PrenotazioneRipetitiva.sala)
    )
    if filtri['gestore_id']:
        query = query.filter(PrenotazioneRipetitiva.gestore_id == filtri['gestore_id'])
    if filtri['stagione_id']:
        query = query.filter(PrenotazioneRipetitiva.stagione_id == filtri['stagione_id'])
    if filtri['genere_id']:
        query = query.filter(PrenotazioneRipetitiva.genere_evento_id == filtri['genere_id'])
    if filtri['sala_id']:
        query = query.filter(PrenotazioneRipetitiva.sala_id == filtri['sala_id'])
    if filtri['categoria_id']:
        query = query.filter(PrenotazioneRipetitiva.categoria_id == filtri['categoria_id'])
    if filtri['q']:
        chiave_ricerca = _normalizza_persona(filtri['q'])
        if chiave_ricerca:
            query = query.filter(PrenotazioneRipetitiva.persona_chiave.contains(chiave_ricerca))
    righe = query.order_by(
        PrenotazioneRipetitiva.persona_chiave, PrenotazioneRipetitiva.stagione_id,
        PrenotazioneRipetitiva.fila, PrenotazioneRipetitiva.colonna
    ).all()

    gruppi = {}
    for r in righe:
        chiave = (r.gestore_id, r.stagione_id, r.genere_evento_id, r.sala_id, r.categoria_id, r.persona_chiave)
        g = gruppi.setdefault(chiave, {'riga': r, 'posti': [], 'attivi': 0})
        g['posti'].append(f"{r.fila}{r.colonna}")
        g['attivi'] += 1 if r.ripetibile else 0
    elenco = []
    for g in gruppi.values():
        r = g['riga']
        n = len(g['posti'])
        elenco.append({
            'riga_id': r.id, 'nome': r.nome_prenotazione, 'categoria': r.categoria, 'gestore': r.gestore,
            'stagione': r.stagione, 'genere': r.genere, 'sala': r.sala, 'posti': _ordina_codici_posto(g['posti']),
            'stato': 'Attivo' if g['attivi'] == n else ('Sospeso' if g['attivi'] == 0 else 'Parziale'),
        })

    # modulo: modifica di un gruppo esistente oppure nuovo (con i filtri come valori iniziali)
    modifica_id = request.args.get('modifica', type=int)
    riga_mod = db.session.get(PrenotazioneRipetitiva, modifica_id) if modifica_id else None
    if riga_mod:
        gruppo_righe = _righe_gruppo(riga_mod)
        iniziale = {
            'gruppo_id': riga_mod.id, 'nome': riga_mod.nome_prenotazione, 'gestore_id': riga_mod.gestore_id,
            'stagione_id': riga_mod.stagione_id, 'genere_id': riga_mod.genere_evento_id, 'sala_id': riga_mod.sala_id,
            'categoria_id': riga_mod.categoria_id, 'posti': _ordina_codici_posto([f"{r.fila}{r.colonna}" for r in gruppo_righe]),
        }
    else:
        iniziale = {
            'gruppo_id': None, 'nome': '', 'gestore_id': filtri['gestore_id'], 'stagione_id': filtri['stagione_id'],
            'genere_id': filtri['genere_id'], 'sala_id': filtri['sala_id'], 'categoria_id': filtri['categoria_id'], 'posti': [],
        }

    dati_form = {
        'stagioni': [{'id': s.id, 'nome': s.nome_stagione, 'gestore_id': s.gestore_id,
                      'periodo': f"{s.data_inizio.strftime('%d/%m/%Y')} - {s.data_fine.strftime('%d/%m/%Y')}"} for s in stagioni],
        'generi': [{'id': g.id, 'nome': g.nome, 'gestore_id': g.gestore_id} for g in generi],
        'categorie': [{'id': c.id, 'nome': c.nome, 'durata': c.durata, 'attiva': c.attiva} for c in categorie],
    }
    return render_template(
        'admin_abbonati.html', gruppi=elenco, gestori=gestori, stagioni=stagioni, generi=generi, sale=sale,
        categorie=categorie, filtri=filtri, iniziale=iniziale, dati_form=dati_form,
        in_modifica=bool(riga_mod)
    )


@app.route('/api/abbonati/mappa')
@login_required
def api_abbonati_mappa():
    """Layout di riferimento e posti gia' presi da altre ripetitive, per la mappa di Gestione Abbonati."""
    if not current_user.is_admin():
        return jsonify({'error': 'Accesso negato'}), 403

    sala_id = request.args.get('sala_id', type=int)
    genere_id = request.args.get('genere_id', type=int)
    gestore_id = request.args.get('gestore_id', type=int)
    stagione_id = request.args.get('stagione_id', type=int)  # assente = categoria permanente
    escludi_id = request.args.get('escludi', type=int)
    if not (sala_id and genere_id and gestore_id):
        return jsonify({'error': 'Parametri mancanti'}), 400

    layout = _layout_per_sala_genere(sala_id, genere_id)
    if not layout:
        return jsonify({'error': 'Nessun layout predefinito configurato per questa sala.'}), 404

    query = PrenotazioneRipetitiva.query.options(joinedload(PrenotazioneRipetitiva.categoria)).filter(
        PrenotazioneRipetitiva.gestore_id == gestore_id,
        PrenotazioneRipetitiva.genere_evento_id == genere_id,
        PrenotazioneRipetitiva.sala_id == sala_id,
        _filtro_stagione(stagione_id)
    )
    if escludi_id:
        riga = db.session.get(PrenotazioneRipetitiva, escludi_id)
        if riga:
            query = query.filter(~PrenotazioneRipetitiva.id.in_([r.id for r in _righe_gruppo(riga)]))

    return jsonify({
        'layout': {
            'nome': layout.nome, 'file': layout.file, 'colonne': layout.colonne,
            'corridoio_colonne': _parse_corridoi_pdf(layout.corridoio_colonne),
            'corridoio_file': _parse_corridoi_pdf(layout.corridoio_file),
        },
        'occupati': [
            {'posto': f"{r.fila}{r.colonna}", 'nome': r.nome_prenotazione, 'categoria': r.categoria.nome,
             'colore': r.categoria.colore, 'sospeso': not r.ripetibile}
            for r in query.all()
        ],
    })


@app.route('/admin/abbonati/save', methods=['POST'])
@login_required
def admin_abbonati_save():
    if not current_user.is_admin():
        abort(403)

    gruppo_id = request.form.get('gruppo_id', type=int)
    riga_rif = db.session.get(PrenotazioneRipetitiva, gruppo_id) if gruppo_id else None
    if gruppo_id and not riga_rif:
        abort(404)
    righe_gruppo = _righe_gruppo(riga_rif) if riga_rif else []
    torna = url_for('admin_abbonati', modifica=gruppo_id) if gruppo_id else url_for('admin_abbonati')

    dati, errore = _leggi_ripetitiva_da_form(categoria_corrente_id=riga_rif.categoria_id if riga_rif else None)
    if errore:
        flash(errore, 'danger')
        return redirect(torna)

    # un posto puo' avere un solo titolare per (gestore, stagione, genere, sala)
    id_gruppo = {r.id for r in righe_gruppo}
    posti_scelti = set(dati['posti'])
    altre = PrenotazioneRipetitiva.query.filter(
        PrenotazioneRipetitiva.gestore_id == dati['gestore_id'],
        PrenotazioneRipetitiva.genere_evento_id == dati['genere_id'],
        PrenotazioneRipetitiva.sala_id == dati['sala_id'],
        _filtro_stagione(dati['stagione_id'])
    ).all()
    conflitti = [r for r in altre if (r.fila, r.colonna) in posti_scelti and r.id not in id_gruppo]
    if conflitti:
        elenco = ', '.join(f"{r.fila}{r.colonna} ({r.nome_prenotazione})" for r in sorted(conflitti, key=lambda r: (r.fila, r.colonna)))
        flash(f"Alcuni posti sono già assegnati ad altri abbonati per questa stagione, genere e sala: {elenco}.", 'danger')
        return redirect(torna)

    try:
        esistenti = {(r.fila, r.colonna): r for r in righe_gruppo}
        attivo = any(r.ripetibile for r in righe_gruppo) if righe_gruppo else True
        da_eliminare = [r for chiave, r in esistenti.items() if chiave not in posti_scelti]
        _elimina_ripetitive(da_eliminare)
        for fila, colonna in dati['posti']:
            riga = esistenti.get((fila, colonna))
            if riga is None:
                riga = PrenotazioneRipetitiva(fila=fila, colonna=colonna, ripetibile=attivo,
                                              origine='manuale', creato_da=current_user.id)
                db.session.add(riga)
            riga.nome_prenotazione = dati['nome']
            riga.persona_chiave = dati['persona_chiave']
            riga.gestore_id = dati['gestore_id']
            riga.categoria_id = dati['categoria_id']
            riga.stagione_id = dati['stagione_id']
            riga.genere_evento_id = dati['genere_id']
            riga.sala_id = dati['sala_id']
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Salvataggio non riuscito: uno dei posti è già assegnato. Ricarica la pagina e riprova.', 'danger')
        return redirect(torna)

    n = len(dati['posti'])
    flash(f"Abbonato '{dati['nome']}' {'aggiornato' if riga_rif else 'creato'}: {n} {'posto' if n == 1 else 'posti'}.", 'success')
    return redirect(url_for('admin_abbonati', gestore_id=dati['gestore_id']))


@app.route('/admin/abbonati/<int:riga_id>/toggle', methods=['POST'])
@login_required
def admin_abbonati_toggle(riga_id):
    """Sospende (o riattiva) tutti i posti dell'abbonato senza cancellarli."""
    if not current_user.is_admin():
        abort(403)
    riga = db.session.get(PrenotazioneRipetitiva, riga_id)
    if not riga:
        abort(404)
    righe = _righe_gruppo(riga)
    nuovo_valore = not any(r.ripetibile for r in righe)
    for r in righe:
        r.ripetibile = nuovo_valore
    db.session.commit()
    flash(f"Abbonato '{riga.nome_prenotazione}' {'riattivato' if nuovo_valore else 'sospeso'}.", 'success')
    return redirect(request.referrer or url_for('admin_abbonati'))


@app.route('/admin/abbonati/<int:riga_id>/delete', methods=['POST'])
@login_required
def admin_abbonati_delete(riga_id):
    if not current_user.is_admin():
        abort(403)
    riga = db.session.get(PrenotazioneRipetitiva, riga_id)
    if not riga:
        abort(404)
    nome = riga.nome_prenotazione
    righe = _righe_gruppo(riga)
    _elimina_ripetitive(righe)
    db.session.commit()
    flash(f"Abbonato '{nome}' eliminato ({len(righe)} posti). Le prenotazioni già caricate sugli eventi non vengono toccate.", 'success')
    return redirect(request.referrer or url_for('admin_abbonati'))


@app.route('/admin/generi')
@login_required
def admin_generi():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    generi = (
        GenereEvento.query
        .outerjoin(Gestore, GenereEvento.gestore_id == Gestore.id)
        .order_by(Gestore.ragione_sociale.asc().nullslast(), GenereEvento.nome.asc())
        .all()
    )
    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    modifica_id = request.args.get('modifica', type=int)
    genere_da_modificare = db.session.get(GenereEvento, modifica_id) if modifica_id else None
    return render_template('admin_generi.html', generi=generi, gestori=gestori, genere_da_modificare=genere_da_modificare)


@app.route('/admin/generi/add', methods=['POST'])
@login_required
def admin_generi_add():
    if not current_user.is_admin():
        abort(403)

    nome = request.form.get('nome', '').strip()
    descrizione = request.form.get('descrizione', '').strip()
    descrizione_aggiuntiva = request.form.get('descrizione_aggiuntiva', '').strip()
    gestore_id = request.form.get('gestore_id', type=int) or None

    if not nome:
        flash('Il nome del genere è obbligatorio.', 'danger')
        return redirect(url_for('admin_generi'))

    if GenereEvento.query.filter(
        func.lower(GenereEvento.nome) == nome.lower(),
        GenereEvento.gestore_id == gestore_id
    ).first():
        flash(f"Esiste già un genere chiamato '{nome}' per questo gestore.", 'danger')
        return redirect(url_for('admin_generi'))

    genere = GenereEvento(
        nome=nome, descrizione=descrizione or None,
        descrizione_aggiuntiva=descrizione_aggiuntiva or None,
        gestore_id=gestore_id,
    )

    errore = _salva_logo_da_form(genere)
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_generi'))

    db.session.add(genere)
    db.session.commit()
    flash(f"Genere '{nome}' creato.", 'success')
    return redirect(url_for('admin_generi'))


@app.route('/admin/generi/<int:genere_id>/edit', methods=['POST'])
@login_required
def admin_generi_edit(genere_id):
    if not current_user.is_admin():
        abort(403)

    genere = db.session.get(GenereEvento, genere_id)
    if not genere:
        abort(404)

    nome = request.form.get('nome', '').strip()
    if not nome:
        flash('Il nome del genere è obbligatorio.', 'danger')
        return redirect(url_for('admin_generi', modifica=genere_id))

    gestore_id = request.form.get('gestore_id', type=int) or None

    duplicato = GenereEvento.query.filter(
        func.lower(GenereEvento.nome) == nome.lower(),
        GenereEvento.gestore_id == gestore_id,
        GenereEvento.id != genere_id
    ).first()
    if duplicato:
        flash(f"Esiste già un genere chiamato '{nome}' per questo gestore.", 'danger')
        return redirect(url_for('admin_generi', modifica=genere_id))

    genere.nome = nome
    genere.descrizione = request.form.get('descrizione', '').strip() or None
    genere.descrizione_aggiuntiva = request.form.get('descrizione_aggiuntiva', '').strip() or None
    genere.gestore_id = gestore_id

    errore = _salva_logo_da_form(genere)
    if errore:
        flash(errore, 'danger')
        return redirect(url_for('admin_generi', modifica=genere_id))

    db.session.commit()
    flash(f"Genere '{nome}' aggiornato.", 'success')
    return redirect(url_for('admin_generi'))


@app.route('/admin/generi/delete/<int:genere_id>', methods=['POST'])
@login_required
def admin_generi_delete(genere_id):
    if not current_user.is_admin():
        abort(403)

    genere = db.session.get(GenereEvento, genere_id)
    if not genere:
        abort(404)

    layout_collegati = LayoutPosti.query.filter_by(genere_evento_id=genere_id).count()
    if layout_collegati > 0:
        flash(
            f"Impossibile eliminare '{genere.nome}': è collegato a {layout_collegati} layout esistenti. "
            f"Scollega prima quei layout.",
            'danger'
        )
        return redirect(url_for('admin_generi'))

    eventi_collegati = Evento.query.filter_by(genere_evento_id=genere_id).count()
    if eventi_collegati > 0:
        flash(
            f"Impossibile eliminare '{genere.nome}': è collegato a {eventi_collegati} eventi esistenti.",
            'danger'
        )
        return redirect(url_for('admin_generi'))

    ripetitive_collegate = PrenotazioneRipetitiva.query.filter_by(genere_evento_id=genere_id).count()
    if ripetitive_collegate > 0:
        flash(
            f"Impossibile eliminare '{genere.nome}': ha {ripetitive_collegate} posti di abbonati collegati.",
            'danger'
        )
        return redirect(url_for('admin_generi'))

    nome = genere.nome
    db.session.delete(genere)
    db.session.commit()
    flash(f"Genere '{nome}' eliminato.", 'success')
    return redirect(url_for('admin_generi'))


def _imposta_layout_default(layout):
    altri = LayoutPosti.query.filter(
        LayoutPosti.sala_id == layout.sala_id,
        LayoutPosti.genere_evento_id == layout.genere_evento_id,
        LayoutPosti.id != layout.id
    ).all()
    for altro in altri:
        altro.is_default = False
    layout.is_default = True


@app.route('/admin/layout-posti')
@login_required
def admin_layout_posti():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    sale = Sala.query.order_by(Sala.nome).all()
    generi = GenereEvento.query.order_by(GenereEvento.nome).all()

    sala_id = request.args.get('sala_id', type=int)
    sala_selezionata = None
    layouts = []
    layout_da_modificare = None

    if sala_id:
        sala_selezionata = db.session.get(Sala, sala_id)
        if sala_selezionata:
            layouts = LayoutPosti.query.filter_by(sala_id=sala_id) \
                .order_by(LayoutPosti.is_default.desc(), LayoutPosti.nome).all()

            modifica_id = request.args.get('modifica', type=int)
            if modifica_id:
                layout_da_modificare = db.session.get(LayoutPosti, modifica_id)
                if not layout_da_modificare or layout_da_modificare.sala_id != sala_id:
                    layout_da_modificare = None

    return render_template(
        'admin_layout_posti.html',
        sale=sale, generi=generi,
        sala_selezionata=sala_selezionata, layouts=layouts,
        layout_da_modificare=layout_da_modificare
    )


@app.route('/admin/layout-posti/add', methods=['POST'])
@login_required
def admin_layout_posti_add():
    if not current_user.is_admin():
        abort(403)

    sala_id = request.form.get('sala_id', type=int)
    genere_evento_id = request.form.get('genere_evento_id', type=int) or None
    nome = request.form.get('nome', '').strip()
    file = request.form.get('file', type=int)
    colonne = request.form.get('colonne', type=int)
    corridoio_colonne = request.form.get('corridoio_colonne', '').strip()
    corridoio_file = request.form.get('corridoio_file', '').strip()
    overbooking_abilitato = request.form.get('overbooking_abilitato') == 'on'
    is_default = request.form.get('is_default') == 'on'

    redirect_url = url_for('admin_layout_posti', sala_id=sala_id)

    if not all([sala_id, nome, file, colonne]):
        flash('Sala, nome, file e colonne sono campi obbligatori.', 'danger')
        return redirect(redirect_url)

    if file < 1 or file > 26 or colonne < 1:
        flash('File deve essere tra 1 e 26, colonne almeno 1.', 'danger')
        return redirect(redirect_url)

    sala = db.session.get(Sala, sala_id)
    if not sala:
        flash('Sala non trovata.', 'danger')
        return redirect(url_for('admin_layout_posti'))

    duplicato = LayoutPosti.query.filter(
        func.lower(LayoutPosti.nome) == nome.lower(),
        LayoutPosti.sala_id == sala_id
    ).first()
    if duplicato:
        flash(f"Esiste gia' un layout chiamato '{nome}' per questa sala.", 'danger')
        return redirect(redirect_url)

    posti_totali = file * colonne
    limite = sala.posti_max + (sala.overbooking_max if overbooking_abilitato else 0)

    if posti_totali > limite:
        if overbooking_abilitato:
            flash(
                f"I posti calcolati ({posti_totali}) superano anche il limite di overbooking "
                f"consentito per questa sala ({limite}).", 'danger'
            )
        else:
            flash(
                f"I posti calcolati ({posti_totali}) superano la capacità della sala ({sala.posti_max}). "
                f"Abilita l'overbooking se vuoi superarla (fino a {sala.posti_max + sala.overbooking_max}).",
                'danger'
            )
        return redirect(redirect_url)

    layout = LayoutPosti(
        sala_id=sala_id,
        genere_evento_id=genere_evento_id,
        nome=nome,
        file=file,
        colonne=colonne,
        corridoio_colonne=corridoio_colonne,
        corridoio_file=corridoio_file,
        overbooking_abilitato=overbooking_abilitato,
        is_default=False,
        creato_da=current_user.id
    )
    db.session.add(layout)
    db.session.flush()

    if is_default:
        _imposta_layout_default(layout)

    db.session.commit()
    flash(f"Layout '{nome}' creato.", 'success')
    return redirect(redirect_url)


@app.route('/admin/layout-posti/edit/<int:layout_id>', methods=['POST'])
@login_required
def admin_layout_posti_edit(layout_id):
    if not current_user.is_admin():
        abort(403)

    layout = db.session.get(LayoutPosti, layout_id)
    if not layout:
        abort(404)

    nome = request.form.get('nome', '').strip()
    file_n = request.form.get('file', type=int)
    colonne = request.form.get('colonne', type=int)
    corridoio_colonne = request.form.get('corridoio_colonne', '').strip()
    corridoio_file = request.form.get('corridoio_file', '').strip()
    overbooking_abilitato = request.form.get('overbooking_abilitato') == 'on'
    genere_evento_id = request.form.get('genere_evento_id', type=int) or None

    redirect_url = url_for('admin_layout_posti', sala_id=layout.sala_id, modifica=layout_id)

    if not all([nome, file_n, colonne]):
        flash('Nome, file e colonne sono campi obbligatori.', 'danger')
        return redirect(redirect_url)

    if file_n < 1 or file_n > 26 or colonne < 1:
        flash('File deve essere tra 1 e 26, colonne almeno 1.', 'danger')
        return redirect(redirect_url)

    duplicato = LayoutPosti.query.filter(
        func.lower(LayoutPosti.nome) == nome.lower(),
        LayoutPosti.sala_id == layout.sala_id,
        LayoutPosti.id != layout_id
    ).first()
    if duplicato:
        flash(f"Esiste gia' un layout chiamato '{nome}' per questa sala.", 'danger')
        return redirect(redirect_url)

    sala = db.session.get(Sala, layout.sala_id)
    posti_totali = file_n * colonne
    limite = sala.posti_max + (sala.overbooking_max if overbooking_abilitato else 0)
    if posti_totali > limite:
        flash(
            f"I posti calcolati ({posti_totali}) superano il limite consentito per questa sala ({limite}).",
            'danger'
        )
        return redirect(redirect_url)

    layout.nome = nome
    layout.file = file_n
    layout.colonne = colonne
    layout.corridoio_colonne = corridoio_colonne
    layout.corridoio_file = corridoio_file
    layout.overbooking_abilitato = overbooking_abilitato
    layout.genere_evento_id = genere_evento_id

    if layout.is_default:
        altri = LayoutPosti.query.filter(
            LayoutPosti.sala_id == layout.sala_id,
            LayoutPosti.genere_evento_id == genere_evento_id,
            LayoutPosti.id != layout_id,
            LayoutPosti.is_default == True
        ).all()
        for altro in altri:
            altro.is_default = False

    db.session.commit()
    flash(f"Layout '{nome}' aggiornato.", 'success')
    return redirect(url_for('admin_layout_posti', sala_id=layout.sala_id))



@app.route('/admin/layout-posti/set-default/<int:layout_id>', methods=['POST'])
@login_required
def admin_layout_posti_set_default(layout_id):
    if not current_user.is_admin():
        abort(403)

    layout = db.session.get(LayoutPosti, layout_id)
    if not layout:
        abort(404)

    _imposta_layout_default(layout)
    db.session.commit()
    flash(f"'{layout.nome}' impostato come layout di default.", 'success')
    return redirect(url_for('admin_layout_posti', sala_id=layout.sala_id))


@app.route('/admin/layout-posti/delete/<int:layout_id>', methods=['POST'])
@login_required
def admin_layout_posti_delete(layout_id):
    if not current_user.is_admin():
        abort(403)

    layout = db.session.get(LayoutPosti, layout_id)
    if not layout:
        abort(404)

    sala_id = layout.sala_id
    nome = layout.nome

    eventi_collegati = Evento.query.filter_by(layout_posti_id=layout_id).count()
    if eventi_collegati > 0:
        flash(
            f"Impossibile eliminare '{nome}': è stato usato per creare {eventi_collegati} eventi. "
            f"Scollega prima quegli eventi se vuoi comunque procedere.",
            'danger'
        )
        return redirect(url_for('admin_layout_posti', sala_id=sala_id))

    try:
        db.session.delete(layout)
        db.session.commit()
        flash(f"Layout '{nome}' eliminato.", 'success')
    except Exception:
        db.session.rollback()
        flash(f"Impossibile eliminare '{nome}': è ancora referenziato altrove.", 'danger')

    return redirect(url_for('admin_layout_posti', sala_id=sala_id))


@app.route('/admin/sale')
@login_required
def admin_sale():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    sale = Sala.query.order_by(Sala.nome).all()
    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    modifica_id = request.args.get('modifica', type=int)
    sala_da_modificare = db.session.get(Sala, modifica_id) if modifica_id else None

    return render_template('admin_sale.html', sale=sale, gestori=gestori, sala_da_modificare=sala_da_modificare)


@app.route('/admin/sale/add', methods=['POST'])
@login_required
def admin_sale_add():
    if not current_user.is_admin():
        abort(403)

    nome = request.form.get('nome', '').strip()
    descrizione = request.form.get('descrizione', '').strip()
    indirizzo = request.form.get('indirizzo', '').strip()
    posti_max = request.form.get('posti_max', type=int)
    overbooking_max = request.form.get('overbooking_max', type=int) or 0
    email_admin = request.form.get('email_admin', '').strip()

    if not nome or not posti_max:
        flash('Nome e capienza massima sono campi obbligatori.', 'danger')
        return redirect(url_for('admin_sale'))

    if posti_max < 1:
        flash('La capienza massima deve essere almeno 1.', 'danger')
        return redirect(url_for('admin_sale'))

    if overbooking_max < 0:
        flash('Il tetto di overbooking non può essere negativo.', 'danger')
        return redirect(url_for('admin_sale'))

    sala = Sala(
        nome=nome,
        descrizione=descrizione or None,
        indirizzo=indirizzo or None,
        posti_max=posti_max,
        overbooking_max=overbooking_max,
        email_admin=email_admin or None,
        gestore_default_id=request.form.get('gestore_default_id', type=int) or None,
    )
    db.session.add(sala)
    db.session.commit()
    flash(f"Sala '{nome}' creata.", 'success')
    return redirect(url_for('admin_sale'))


@app.route('/admin/sale/edit/<int:sala_id>', methods=['POST'])
@login_required
def admin_sale_edit(sala_id):
    if not current_user.is_admin():
        abort(403)

    sala = db.session.get(Sala, sala_id)
    if not sala:
        abort(404)

    nome = request.form.get('nome', '').strip()
    descrizione = request.form.get('descrizione', '').strip()
    indirizzo = request.form.get('indirizzo', '').strip()
    posti_max = request.form.get('posti_max', type=int)
    overbooking_max = request.form.get('overbooking_max', type=int) or 0
    email_admin = request.form.get('email_admin', '').strip()

    if not nome or not posti_max:
        flash('Nome e capienza massima sono campi obbligatori.', 'danger')
        return redirect(url_for('admin_sale', modifica=sala_id))

    if posti_max < 1:
        flash('La capienza massima deve essere almeno 1.', 'danger')
        return redirect(url_for('admin_sale', modifica=sala_id))

    if overbooking_max < 0:
        flash('Il tetto di overbooking non può essere negativo.', 'danger')
        return redirect(url_for('admin_sale', modifica=sala_id))

    max_layout_esistente = db.session.query(func.max(LayoutPosti.file * LayoutPosti.colonne)) \
        .filter(LayoutPosti.sala_id == sala_id).scalar()
    nuovo_limite = posti_max + overbooking_max
    if max_layout_esistente and max_layout_esistente > nuovo_limite:
        flash(
            f"Attenzione: esiste già un layout per questa sala con {max_layout_esistente} posti, "
            f"superiore al nuovo limite ({nuovo_limite}). Il layout resta invariato, ma non potrai "
            f"crearne altri sopra il nuovo limite finché non lo alzi di nuovo.",
            'warning'
        )

    sala.nome = nome
    sala.descrizione = descrizione or None
    sala.indirizzo = indirizzo or None
    sala.posti_max = posti_max
    sala.overbooking_max = overbooking_max
    sala.email_admin = email_admin or None
    sala.gestore_default_id = request.form.get('gestore_default_id', type=int) or None
    db.session.commit()
    flash(f"Sala '{nome}' aggiornata.", 'success')
    return redirect(url_for('admin_sale'))


@app.route('/admin/sale/delete/<int:sala_id>', methods=['POST'])
@login_required
def admin_sale_delete(sala_id):
    if not current_user.is_admin():
        abort(403)

    sala = db.session.get(Sala, sala_id)
    if not sala:
        abort(404)

    eventi_collegati = Evento.query.filter_by(sala_id=sala_id).count()
    layout_collegati = LayoutPosti.query.filter_by(sala_id=sala_id).count()

    if eventi_collegati > 0:
        flash(
            f"Impossibile eliminare '{sala.nome}': ha {eventi_collegati} eventi collegati. "
            f"Elimina prima gli eventi se vuoi procedere.",
            'danger'
        )
        return redirect(url_for('admin_sale'))

    if layout_collegati > 0:
        flash(
            f"Impossibile eliminare '{sala.nome}': ha {layout_collegati} layout posti collegati. "
            f"Eliminali prima dalla Gestione Layout Posti.",
            'danger'
        )
        return redirect(url_for('admin_sale'))

    ripetitive_collegate = PrenotazioneRipetitiva.query.filter_by(sala_id=sala_id).count()
    if ripetitive_collegate > 0:
        flash(
            f"Impossibile eliminare '{sala.nome}': ha {ripetitive_collegate} posti di abbonati collegati. "
            f"Eliminali prima da Gestione Abbonati.",
            'danger'
        )
        return redirect(url_for('admin_sale'))

    nome = sala.nome
    db.session.delete(sala)
    db.session.commit()
    flash(f"Sala '{nome}' eliminata.", 'success')
    return redirect(url_for('admin_sale'))


@app.route('/guida')
@login_required
def guida():
    return render_template('guida_event_booking.html')


# ==============================================================================
# 9. INTEGRAZIONE GOOGLE CALENDAR (OAUTH & SYNC)
# ==============================================================================

GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
]

FUSO_ORARIO_APP = ZoneInfo('Europe/Rome')


def _google_configurato():
    return bool(app.config.get('GOOGLE_CLIENT_ID') and app.config.get('GOOGLE_CLIENT_SECRET')
                and _google_redirect_uri() and app.config.get('TOKEN_ENCRYPTION_KEY'))


def _google_redirect_uri():
    """GOOGLE_REDIRECT_URI senza spazi/tab iniziali o finali (un incolla dalla tabella delle variabili
    d'ambiente puo' portarsi dietro un carattere invisibile e il valore non coinciderebbe piu' con Google)."""
    return (app.config.get('GOOGLE_REDIRECT_URI') or '').strip()


def _diagnosi_redirect_uri():
    """Segnala incoerenze evidenti nell'URI di reindirizzamento. Ritorna (valore, avviso o None)."""
    grezzo = app.config.get('GOOGLE_REDIRECT_URI') or ''
    uri = grezzo.strip()
    if not uri:
        return '', None
    avvisi = []
    if grezzo != uri:
        avvisi.append("Il valore salvato contiene spazi o tabulazioni all'inizio o alla fine: l'app li ignora, "
                      "ma conviene correggere la variabile d'ambiente.")
    parti = urlparse(uri)
    if parti.netloc and parti.netloc != request.host:
        avvisi.append(f"L'indirizzo dell'URI ({parti.netloc}) è diverso da quello da cui stai usando l'app "
                      f"({request.host}): dopo il consenso Google ti riporterebbe su un altro sito e il collegamento "
                      f"non si completerebbe. Su ogni servizio la variabile deve puntare al proprio indirizzo.")
    if parti.path != '/admin/google/callback':
        avvisi.append("Il percorso dovrebbe essere /admin/google/callback.")
    if request.is_secure and parti.scheme != 'https':
        avvisi.append("L'URI dovrebbe iniziare con https://.")
    return uri, (' '.join(avvisi) if avvisi else None)


def _google_client_config():
    return {
        "web": {
            "client_id": app.config['GOOGLE_CLIENT_ID'],
            "client_secret": app.config['GOOGLE_CLIENT_SECRET'],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [_google_redirect_uri()],
        }
    }


def _fernet():
    key = app.config.get('TOKEN_ENCRYPTION_KEY')
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY non configurata.")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _cifra_token(token_plain):
    return _fernet().encrypt(token_plain.encode()).decode()


def _decifra_token(token_cifrato):
    return _fernet().decrypt(token_cifrato.encode()).decode()


def _connessione_google_attiva():
    return GoogleConnessione.query.order_by(GoogleConnessione.data_connessione.desc()).first()


def _credenziali_google(connessione):
    refresh_token = _decifra_token(connessione.refresh_token_cifrato)
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        scopes=GOOGLE_SCOPES,
    )


def _errore_google_leggibile(e, contesto):
    """Traduce in italiano l'errore piu' comune: refresh token scaduto o revocato (invalid_grant)."""
    if isinstance(e, RefreshError) or 'invalid_grant' in str(e):
        return (
            "L'autorizzazione di Google non è più valida: è scaduta, è stata revocata, oppure è stata rilasciata "
            "a un client OAuth diverso da quello configurato ora (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET). "
            "Nella pagina Google Calendar scollega l'account e ricollegalo (Scollega, poi Collega Account Google). "
            "Se capita di nuovo dopo circa una settimana, la schermata di consenso OAuth del progetto "
            "Google è ancora in modalità \"Test\" e va pubblicata (\"In produzione\")."
        )
    return f"{contesto}: {e}"


def _elenca_calendari_google():
    connessione = _connessione_google_attiva()
    if not connessione:
        return None, None
    try:
        creds = _credenziali_google(connessione)
        service = google_build('calendar', 'v3', credentials=creds)
        risultato = service.calendarList().list(maxResults=250).execute()
        connessione.ultimo_utilizzo = datetime.utcnow()
        db.session.commit()
        return risultato.get('items', []), None
    except GoogleHttpError as e:
        return None, f"Errore dall'API Google: {e}"
    except Exception as e:
        return None, _errore_google_leggibile(e, "Errore durante il recupero dei calendari")


@app.route('/admin/google')
@login_required
def admin_google_status():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    if not _google_configurato():
        flash(
            "Google Calendar non è ancora configurato sul server: mancano una o più variabili d'ambiente.",
            'warning'
        )

    connessione = _connessione_google_attiva()
    calendari = None
    errore_calendari = None

    if connessione and request.args.get('test') == '1':
        try:
            creds = _credenziali_google(connessione)
            service = google_build('calendar', 'v3', credentials=creds)
            risultato = service.calendarList().list(maxResults=50).execute()
            calendari = risultato.get('items', [])
            connessione.ultimo_utilizzo = datetime.utcnow()
            db.session.commit()
        except GoogleHttpError as e:
            errore_calendari = f"Errore dall'API Google: {e}"
        except Exception as e:
            errore_calendari = _errore_google_leggibile(e, "Errore durante il test")

    redirect_uri_config, redirect_uri_avviso = _diagnosi_redirect_uri()
    return render_template(
        'admin_google.html',
        connessione=connessione, calendari=calendari, errore_calendari=errore_calendari,
        google_configurato=_google_configurato(),
        redirect_uri_config=redirect_uri_config, redirect_uri_avviso=redirect_uri_avviso
    )


@app.route('/admin/google/connect')
@login_required
def admin_google_connect():
    if not current_user.is_admin():
        abort(403)
    if not _google_configurato():
        flash('Configurazione Google mancante sul server. Contatta chi gestisce il deploy.', 'danger')
        return redirect(url_for('admin_google_status'))

    flow = Flow.from_client_config(
        _google_client_config(), scopes=GOOGLE_SCOPES,
        redirect_uri=_google_redirect_uri()
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )
    session['google_oauth_state'] = state
    return redirect(authorization_url)


@app.route('/admin/google/callback')
@login_required
def admin_google_callback():
    if not current_user.is_admin():
        abort(403)

    stato_atteso = session.pop('google_oauth_state', None)
    stato_ricevuto = request.args.get('state')
    if not stato_atteso or stato_atteso != stato_ricevuto:
        flash('Sessione OAuth non valida o scaduta. Riprova la connessione.', 'danger')
        return redirect(url_for('admin_google_status'))

    if request.args.get('error'):
        flash(f"Autorizzazione negata da Google: {request.args.get('error')}", 'warning')
        return redirect(url_for('admin_google_status'))

    try:
        flow = Flow.from_client_config(
            _google_client_config(), scopes=GOOGLE_SCOPES,
            redirect_uri=_google_redirect_uri()
        )
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        if not creds.refresh_token:
            flash(
                "Google non ha restituito un refresh_token. Prova a scollegare l'account e ripetere la connessione.",
                'danger'
            )
            return redirect(url_for('admin_google_status'))

        userinfo = http_requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {creds.token}'}, timeout=10
        ).json()
        email_google = userinfo.get('email', 'sconosciuta')

        GoogleConnessione.query.delete()

        connessione = GoogleConnessione(
            utente_id=current_user.id,
            email_google=email_google,
            refresh_token_cifrato=_cifra_token(creds.refresh_token),
            scopes=' '.join(creds.scopes or GOOGLE_SCOPES),
        )
        db.session.add(connessione)
        db.session.commit()
        flash(f"Account Google collegato con successo: {email_google}", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Errore durante il collegamento a Google: {str(e)}", 'danger')

    return redirect(url_for('admin_google_status'))


@app.route('/admin/google/disconnect', methods=['POST'])
@login_required
def admin_google_disconnect():
    if not current_user.is_admin():
        abort(403)

    connessione = _connessione_google_attiva()
    if connessione:
        try:
            refresh_token = _decifra_token(connessione.refresh_token_cifrato)
            http_requests.post(
                'https://oauth2.googleapis.com/revoke',
                params={'token': refresh_token},
                headers={'content-type': 'application/x-www-form-urlencoded'}, timeout=10
            )
        except Exception:
            pass

        db.session.delete(connessione)
        db.session.commit()
        flash('Account Google scollegato.', 'success')
    else:
        flash('Nessun account Google era collegato.', 'info')

    return redirect(url_for('admin_google_status'))


@app.route('/admin/google/sale')
@login_required
def admin_google_sale():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    if not _google_configurato():
        flash('Google Calendar non è ancora configurato sul server.', 'warning')
        return redirect(url_for('admin_google_status'))

    connessione = _connessione_google_attiva()
    if not connessione:
        flash('Collega prima un account Google prima di associare i calendari alle sale.', 'warning')
        return redirect(url_for('admin_google_status'))

    calendari, errore_calendari = _elenca_calendari_google()
    sale = Sala.query.order_by(Sala.nome).all()
    associazioni = {a.sala_id: a for a in CalendarioGoogle.query.all()}

    return render_template(
        'admin_google_sale.html',
        sale=sale, associazioni=associazioni,
        calendari=calendari, errore_calendari=errore_calendari
    )


@app.route('/admin/google/sale/<int:sala_id>/associa', methods=['POST'])
@login_required
def admin_google_sale_associa(sala_id):
    if not current_user.is_admin():
        abort(403)

    sala = db.session.get(Sala, sala_id)
    if not sala:
        abort(404)

    google_calendar_id = (request.form.get('google_calendar_id') or '').strip()
    if not google_calendar_id:
        flash('Seleziona un calendario da associare.', 'warning')
        return redirect(url_for('admin_google_sale'))

    nome_calendario = (request.form.get('nome_calendario') or google_calendar_id).strip()

    associazione = CalendarioGoogle.query.filter_by(sala_id=sala_id).first()
    if associazione:
        associazione.google_calendar_id = google_calendar_id
        associazione.nome_calendario = nome_calendario
        associazione.attivo = True
    else:
        associazione = CalendarioGoogle(
            sala_id=sala_id,
            google_calendar_id=google_calendar_id,
            nome_calendario=nome_calendario,
            creato_da=current_user.id,
        )
        db.session.add(associazione)

    db.session.commit()
    flash(f'Calendario "{nome_calendario}" associato alla sala "{sala.nome}".', 'success')
    return redirect(url_for('admin_google_sale'))


@app.route('/admin/google/sale/<int:sala_id>/rimuovi', methods=['POST'])
@login_required
def admin_google_sale_rimuovi(sala_id):
    if not current_user.is_admin():
        abort(403)

    associazione = CalendarioGoogle.query.filter_by(sala_id=sala_id).first()
    if associazione:
        db.session.delete(associazione)
        db.session.commit()
        flash('Associazione rimossa.', 'success')
    else:
        flash('Nessuna associazione da rimuovere per questa sala.', 'info')

    return redirect(url_for('admin_google_sale'))


def _parse_datetime_google(valore_iso):
    return datetime.fromisoformat(valore_iso.replace('Z', '+00:00'))


def _layout_default_per_sala(sala_id):
    layout = LayoutPosti.query.filter_by(sala_id=sala_id, genere_evento_id=None, is_default=True).first()
    if not layout:
        layout = LayoutPosti.query.filter_by(sala_id=sala_id, is_default=True).first()
    return layout


def _crea_posti_griglia(evento):
    numero = 1
    posti_bulk = []
    for f in range(1, evento.file + 1):
        fila_lettera = chr(64 + f)
        for c in range(1, evento.colonne + 1):
            posti_bulk.append(Posto(
                sala_id=evento.sala_id, evento_id=evento.id, numero_posto=numero,
                fila=fila_lettera, colonna=c, stato='libero'
            ))
            numero += 1
    db.session.add_all(posti_bulk)


def _crea_evento_da_import(sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, google_event_id, gestore_id=None, genere_evento_id_scelto=None):
    layout = _layout_default_per_sala(sala_id)
    if not layout:
        return None, 'nessun layout di default configurato per questa sala'

    sala = db.session.get(Sala, sala_id)
    posti_max = layout.file * layout.colonne
    limite = sala.posti_max + (sala.overbooking_max if layout.overbooking_abilitato else 0)
    if posti_max > limite:
        return None, f'il layout di default ({posti_max} posti) supera la capacità della sala ({limite})'

    genere_finale = genere_evento_id_scelto if genere_evento_id_scelto is not None else layout.genere_evento_id

    evento = Evento(
        nome=nome, descrizione=descrizione, data_evento=data_obj, ora_inizio=ora_obj,
        durata=durata, posti_max=posti_max, file=layout.file, colonne=layout.colonne,
        corridoio_colonne=layout.corridoio_colonne, corridoio_file=layout.corridoio_file,
        sala_id=sala_id, creato_da=current_user.id,
        layout_posti_id=layout.id, genere_evento_id=genere_finale,
        gestore_id=gestore_id,
        overbooking_abilitato=layout.overbooking_abilitato,
        origine='google', google_event_id=google_event_id,
        google_calendar_id_origine=calendar_id if google_event_id else None,
        google_updated=datetime.utcnow(),
    )
    db.session.add(evento)
    db.session.flush()
    _crea_posti_griglia(evento)
    return evento, None


def _recupera_eventi_google_range(service, calendar_id, giorni):
    ora = datetime.now(FUSO_ORARIO_APP)
    time_min = ora.isoformat()
    time_max = (ora + timedelta(days=giorni)).isoformat()

    eventi, page_token = [], None
    while True:
        risultato = service.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime', maxResults=250, pageToken=page_token
        ).execute()
        eventi.extend(risultato.get('items', []))
        page_token = risultato.get('nextPageToken')
        if not page_token:
            break
    return eventi


def _normalizza_evento_google(item):
    start, end = item.get('start', {}), item.get('end', {})
    if 'dateTime' not in start or 'dateTime' not in end:
        return None

    inizio = _parse_datetime_google(start['dateTime']).astimezone(FUSO_ORARIO_APP)
    fine = _parse_datetime_google(end['dateTime']).astimezone(FUSO_ORARIO_APP)
    durata_minuti = max(1, round((fine - inizio).total_seconds() / 60))

    return {
        'google_event_id': item['id'],
        'nome': item.get('summary') or '(senza titolo)',
        'descrizione': item.get('description', '') or '',
        'data_evento': inizio.date(),
        'ora_inizio': inizio.time().replace(second=0, microsecond=0),
        'durata': durata_minuti,
        'link_google': item.get('htmlLink'),
    }


@app.route('/admin/google/import', methods=['GET'])
@login_required
def admin_google_import():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    if not _google_configurato():
        flash('Google Calendar non è ancora configurato sul server.', 'warning')
        return redirect(url_for('admin_google_status'))

    connessione = _connessione_google_attiva()
    if not connessione:
        flash('Collega prima un account Google.', 'warning')
        return redirect(url_for('admin_google_status'))

    calendari, errore_calendari = _elenca_calendari_google()

    calendar_id = request.args.get('calendar_id', '').strip()
    try:
        giorni = int(request.args.get('giorni', 60))
    except (TypeError, ValueError):
        giorni = 60
    giorni = max(1, min(giorni, 365))
    gestore_id_import = request.args.get('gestore_id', type=int) or None

    sale = Sala.query.order_by(Sala.nome).all()
    gestori = Gestore.query.order_by(Gestore.ragione_sociale).all()
    if gestore_id_import:
        generi_disponibili = GenereEvento.query.filter_by(gestore_id=gestore_id_import).order_by(GenereEvento.nome).all()
    else:
        generi_disponibili = GenereEvento.query.filter_by(gestore_id=None).order_by(GenereEvento.nome).all()
    righe_nuovi, righe_modificati, righe_invariati = [], [], []
    righe_non_importabili, righe_cancellati = [], []
    errore_import = None

    if calendar_id:
        try:
            creds = _credenziali_google(connessione)
            service = google_build('calendar', 'v3', credentials=creds)
            eventi_google = _recupera_eventi_google_range(service, calendar_id, giorni)
            connessione.ultimo_utilizzo = datetime.utcnow()
            db.session.commit()

            associazione = CalendarioGoogle.query.filter_by(google_calendar_id=calendar_id, attivo=True).first()
            sala_suggerita_id = associazione.sala_id if associazione else None

            id_trovati = set()
            for item in eventi_google:
                if item.get('status') == 'cancelled':
                    continue
                dati = _normalizza_evento_google(item)
                if dati is None:
                    righe_non_importabili.append({
                        'nome': item.get('summary') or '(senza titolo)',
                        'motivo': 'Evento "intera giornata" (senza orario): non supportato in questa versione.',
                        'link_google': item.get('htmlLink'),
                    })
                    continue

                id_trovati.add(dati['google_event_id'])
                matched = Evento.query.filter_by(
                    google_event_id=dati['google_event_id'], google_calendar_id_origine=calendar_id
                ).first()

                riga = dict(dati)
                if matched:
                    differenze = []
                    if matched.nome != dati['nome']:
                        differenze.append(('Nome', matched.nome, dati['nome']))
                    if matched.data_evento != dati['data_evento']:
                        differenze.append(('Data', matched.data_evento.strftime('%d/%m/%Y'), dati['data_evento'].strftime('%d/%m/%Y')))
                    if matched.ora_inizio != dati['ora_inizio']:
                        differenze.append(('Ora', matched.ora_inizio.strftime('%H:%M'), dati['ora_inizio'].strftime('%H:%M')))
                    if matched.durata != dati['durata']:
                        differenze.append(('Durata (min)', matched.durata, dati['durata']))

                    riga['evento_id'] = matched.id
                    riga['sala_attuale'] = matched.sala.nome
                    riga['sala_attuale_id'] = matched.sala_id
                    riga['genere_attuale_id'] = matched.genere_evento_id
                    riga['ha_prenotazioni'] = len(matched.prenotazioni) > 0

                    (righe_modificati if differenze else righe_invariati).append(riga)
                    riga['differenze'] = differenze
                else:
                    riga['sala_suggerita'] = sala_suggerita_id
                    righe_nuovi.append(riga)

            oggi = date.today()
            fine_periodo = oggi + timedelta(days=giorni)
            candidati_cancellati = Evento.query.filter(
                Evento.google_calendar_id_origine == calendar_id,
                Evento.origine == 'google',
                Evento.cancellato_google == False,
                Evento.data_evento >= oggi,
                Evento.data_evento <= fine_periodo,
            ).all()
            for ev in candidati_cancellati:
                if ev.google_event_id not in id_trovati:
                    righe_cancellati.append({
                        'evento_id': ev.id, 'nome': ev.nome, 'data_evento': ev.data_evento,
                        'ora_inizio': ev.ora_inizio, 'sala': ev.sala.nome,
                        'ha_prenotazioni': len(ev.prenotazioni) > 0,
                    })

        except GoogleHttpError as e:
            errore_import = f"Errore dall'API Google: {e}"
        except Exception as e:
            errore_import = _errore_google_leggibile(e, "Errore durante il recupero degli eventi")

    return render_template(
        'admin_google_import.html',
        calendari=calendari, errore_calendari=errore_calendari,
        calendar_id=calendar_id, giorni=giorni, sale=sale,
        gestori=gestori, gestore_id_import=gestore_id_import, generi_disponibili=generi_disponibili,
        righe_nuovi=righe_nuovi, righe_modificati=righe_modificati,
        righe_invariati=righe_invariati, righe_non_importabili=righe_non_importabili,
        righe_cancellati=righe_cancellati, errore_import=errore_import,
    )


@app.route('/admin/google/import/applica', methods=['POST'])
@login_required
def admin_google_import_applica():
    if not current_user.is_admin():
        abort(403)

    calendar_id = request.form.get('calendar_id', '').strip()
    giorni = request.form.get('giorni', type=int) or 60
    gestore_id_import = request.form.get('gestore_id', type=int) or None
    gids = request.form.getlist('gid')

    contatori = {'importati': 0, 'aggiornati': 0, 'sostituiti': 0, 'mantenuti_entrambi': 0, 'ignorati': 0, 'errori': 0}

    for gid in gids:
        azione = request.form.get(f'azione__{gid}', 'ignora')
        if azione == 'ignora':
            contatori['ignorati'] += 1
            continue

        nome = request.form.get(f'nome__{gid}', '').strip()
        descrizione = request.form.get(f'descrizione__{gid}', '').strip()
        data_str = request.form.get(f'data__{gid}', '')
        ora_str = request.form.get(f'ora__{gid}', '')
        durata = request.form.get(f'durata__{gid}', type=int)
        sala_id = request.form.get(f'sala__{gid}', type=int)
        evento_id_esistente = request.form.get(f'evento_id__{gid}', type=int)
        genere_scelto = request.form.get(f'genere__{gid}', type=int) or None

        try:
            data_obj = datetime.strptime(data_str, '%Y-%m-%d').date()
            ora_obj = datetime.strptime(ora_str, '%H:%M').time()
        except ValueError:
            contatori['errori'] += 1
            flash(f'Riga "{nome}": data/ora non valide, saltata.', 'danger')
            continue

        if not sala_id:
            contatori['errori'] += 1
            flash(f'Riga "{nome}": nessuna sala di destinazione selezionata, saltata.', 'danger')
            continue

        if azione == 'aggiorna':
            evento = db.session.get(Evento, evento_id_esistente) if evento_id_esistente else None
            if not evento:
                contatori['errori'] += 1
                flash(f'Riga "{nome}": evento da aggiornare non trovato, saltata.', 'danger')
                continue
            evento.nome = nome
            evento.descrizione = descrizione
            evento.data_evento = data_obj
            evento.ora_inizio = ora_obj
            evento.durata = durata
            evento.sala_id = sala_id
            evento.gestore_id = gestore_id_import
            evento.genere_evento_id = genere_scelto
            evento.google_updated = datetime.utcnow()
            contatori['aggiornati'] += 1

        elif azione == 'sostituisci':
            evento_vecchio = db.session.get(Evento, evento_id_esistente) if evento_id_esistente else None
            if evento_vecchio:
                db.session.delete(evento_vecchio)
                db.session.flush()
            _, errore = _crea_evento_da_import(
                sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, gid,
                gestore_id=gestore_id_import, genere_evento_id_scelto=genere_scelto
            )
            if errore:
                contatori['errori'] += 1
                flash(f'Riga "{nome}": {errore}, impossibile creare l\'evento.', 'danger')
                continue
            contatori['sostituiti'] += 1

        elif azione == 'importa_nuovo':
            _, errore = _crea_evento_da_import(
                sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, gid,
                gestore_id=gestore_id_import, genere_evento_id_scelto=genere_scelto
            )
            if errore:
                contatori['errori'] += 1
                flash(f'Riga "{nome}": {errore}, impossibile creare l\'evento.', 'danger')
                continue
            contatori['importati'] += 1

        elif azione == 'mantieni_entrambi':
            _, errore = _crea_evento_da_import(
                sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, None,
                gestore_id=gestore_id_import, genere_evento_id_scelto=genere_scelto
            )
            if errore:
                contatori['errori'] += 1
                flash(f'Riga "{nome}": {errore}, impossibile creare l\'evento.', 'danger')
                continue
            contatori['mantenuti_entrambi'] += 1

    for evento_id_str in request.form.getlist('cancel_evento_id'):
        evento_id = int(evento_id_str)
        azione_cancel = request.form.get(f'azione_cancel__{evento_id}', 'ignora')
        evento = db.session.get(Evento, evento_id)
        if not evento:
            continue
        if azione_cancel == 'annulla':
            evento.cancellato_google = True
        elif azione_cancel == 'elimina':
            db.session.delete(evento)

    db.session.commit()

    riepilogo = (
        f"Import completato — nuovi: {contatori['importati']}, aggiornati: {contatori['aggiornati']}, "
        f"sostituiti: {contatori['sostituiti']}, mantenuti entrambi: {contatori['mantenuti_entrambi']}, "
        f"ignorati: {contatori['ignorati']}" + (f", errori: {contatori['errori']}" if contatori['errori'] else "")
    )
    flash(riepilogo, 'success' if not contatori['errori'] else 'warning')
    return redirect(url_for('admin_google_import', calendar_id=calendar_id, giorni=giorni, gestore_id=gestore_id_import))


# ==============================================================================
# 10. GENERAZIONE DOCUMENTALE PDF (REPORTLAB: A4 orizzontale per la pianta, A4 verticale per l'elenco)
# ==============================================================================

def _parse_corridoi_pdf(valore):
    if not valore:
        return []
    return [int(x.strip()) for x in valore.split(',') if x.strip().isdigit()]


def _pdf_righe_nome(testo, font, size, max_w, max_righe=2):
    """Spezza il nome su al massimo `max_righe` righe larghe non oltre `max_w` punti.
    Va a capo sugli spazi; una parola piu' larga della cella viene spezzata per caratteri;
    se il testo non entra, l'ultima riga termina con '...'."""
    testo = ' '.join((testo or '').split())
    if not testo:
        return []
    righe = []
    for riga in simpleSplit(testo, font, size, max_w):
        while stringWidth(riga, font, size) > max_w and len(riga) > 1:
            n = len(riga)
            while n > 1 and stringWidth(riga[:n], font, size) > max_w:
                n -= 1
            righe.append(riga[:n])
            riga = riga[n:]
        righe.append(riga)
    if len(righe) > max_righe:
        righe = righe[:max_righe]
        ultima = righe[-1]
        while ultima and stringWidth(ultima + '...', font, size) > max_w:
            ultima = ultima[:-1]
        righe[-1] = ultima + '...'
    return righe


def _pdf_disegna_posto(c, x, y, cella_w, cella_h, riferimento, occupato, nome):
    """Disegna una cella della pianta: riferimento del posto (es. B7) in alto a sinistra,
    grande e in grassetto; nome della prenotazione in basso, su due righe se c'e' spazio.
    I caratteri si adattano alla dimensione della cella e, se serve, si riducono un po'
    finche' il nome entra per intero (senza '...' e senza parole spezzate)."""
    pad = 1.5 * mm
    ref_font = max(7, min(18, cella_h * 0.32, cella_w * 0.30))
    nome_max = max(5.5, min(13, cella_h * 0.23, cella_w * 0.16))
    larghezza_utile = cella_w - 2 * pad

    c.setFillColor(colors.HexColor('#dbeafe') if occupato else colors.white)
    c.rect(x, y - cella_h, cella_w, cella_h, stroke=1, fill=1)

    c.setFillColor(colors.black)
    c.setFont('Helvetica-Bold', ref_font)
    c.drawString(x + pad, y - 1.2 * mm - ref_font * 0.78, riferimento)

    if not (occupato and nome):
        return

    testo_pieno = ''.join(nome.split())
    nome_font = nome_max
    while True:
        leading = nome_font * 1.1
        spazio_nome = cella_h - (1.2 * mm + ref_font) - 1.2 * mm
        max_righe = 2 if spazio_nome >= 2 * leading else 1
        righe = _pdf_righe_nome(nome, 'Helvetica-Bold', nome_font, larghezza_utile, max_righe)
        troncato = ''.join(righe).replace(' ', '') != testo_pieno
        parola_spezzata = any(len(w) > 1 and stringWidth(w, 'Helvetica-Bold', nome_font) > larghezza_utile
                              for w in nome.split())
        if (not troncato and not parola_spezzata) or nome_font <= 5.5:
            break
        nome_font -= 0.25

    c.setFont('Helvetica-Bold', nome_font)
    # l'ultima riga poggia sul fondo della cella, le precedenti salgono
    for i, riga in enumerate(reversed(righe)):
        c.drawString(x + pad, y - cella_h + 1.2 * mm + nome_font * 0.2 + i * leading, riga)


def _pdf_elenco_a4(c, evento, prenotazioni):
    """Elenco prenotazioni su A4 verticale, in due colonne (Arrivo | Nome | Posti).
    Sceglie il carattere piu' grande che permette di stare in UNA sola pagina (da 16 a 8 pt);
    solo se nemmeno a 8 pt basta, continua su pagine successive."""
    pw, ph = portrait(A4)
    c.setPageSize((pw, ph))
    marg = 10 * mm
    gap_col = 8 * mm
    col_w = (pw - 2 * marg - gap_col) / 2
    bottom = 10 * mm
    top_corpo = ph - 26 * mm

    voci = []
    for p in prenotazioni:
        nome = p.nome_prenotazione or p.utente.nome_cognome or ''
        posti_ord = sorted(p.posti, key=lambda s: (s.fila, s.colonna))
        voci.append((nome, ', '.join(f"{s.fila}{s.colonna}" for s in posti_ord), bool(p.presente)))
    voci.sort(key=lambda v: v[0].casefold())

    titolo = f"Elenco prenotazioni — {evento.nome}"
    tf = 16
    while tf > 11 and stringWidth(titolo, 'Helvetica-Bold', tf) > pw - 2 * marg:
        tf -= 0.5
    sottotitolo = (f"{evento.sala.nome} — {evento.data_evento.strftime('%d/%m/%Y')} "
                   f"{evento.ora_inizio.strftime('%H:%M')} — {len(voci)} prenotazioni")

    def geometria(f):
        box = f * 0.9
        hf = f * 0.62                      # carattere delle intestazioni di colonna
        sp = f * 0.5
        col_arrivo = max(box, stringWidth('Arrivo', 'Helvetica-Bold', hf)) + sp
        utile = col_w - col_arrivo - sp
        return box, hf, sp, col_arrivo, utile * 0.46, utile * 0.54

    def costruisci_righe(f):
        box, hf, sp, col_arrivo, w_nome, w_posti = geometria(f)
        righe = []
        for nome, posti_str, presente in voci:
            fn = f
            parole = nome.split()
            # un cognome lungo riduce un po' il carattere di quella riga, senza spezzarsi a meta'
            while parole and fn > f * 0.7 and max(stringWidth(w, 'Helvetica', fn) for w in parole) > w_nome:
                fn -= 0.25
            r_nome = _pdf_righe_nome(nome, 'Helvetica', fn, w_nome, 3)
            r_posti = _pdf_righe_nome(posti_str, 'Helvetica', f, w_posti, 6)
            h = max(f * 1.9, len(r_nome) * fn * 1.15 + 5, len(r_posti) * f * 1.15 + 5)
            righe.append({'r_nome': r_nome, 'fn': fn, 'r_posti': r_posti, 'presente': presente, 'h': h})
        return righe

    def impagina(righe, f):
        disponibile = top_corpo - bottom - f * 1.9
        pagine = [[[]]]                    # pagine -> colonne -> righe
        usato = 0
        for r in righe:
            if usato + r['h'] > disponibile and pagine[-1][-1]:
                if len(pagine[-1]) == 1:
                    pagine[-1].append([])
                else:
                    pagine.append([[]])
                usato = 0
            pagine[-1][-1].append(r)
            usato += r['h']
        return pagine

    f = 16.0
    while True:
        righe = costruisci_righe(f)
        pagine = impagina(righe, f)
        if len(pagine) == 1 or f <= 8.0:
            break
        f -= 0.5

    box, hf, sp, col_arrivo, w_nome, w_posti = geometria(f)
    hh = f * 1.9
    for i, pagina in enumerate(pagine):
        if i > 0:
            c.showPage()
            c.setPageSize((pw, ph))
        c.setFillColor(colors.black)
        c.setFont('Helvetica-Bold', tf)
        c.drawString(marg, ph - 12 * mm, titolo)
        c.setFont('Helvetica', 10)
        c.drawString(marg, ph - 18 * mm, sottotitolo)
        for ci, colonna in enumerate(pagina):
            x0 = marg + ci * (col_w + gap_col)
            c.setFont('Helvetica-Bold', hf)
            base_h = top_corpo - hh * 0.65
            c.drawString(x0, base_h, "Arrivo")
            c.drawString(x0 + col_arrivo, base_h, "Nome")
            c.drawString(x0 + col_arrivo + w_nome + sp, base_h, "Posti")
            c.setStrokeColor(colors.black)
            c.setLineWidth(1)
            c.line(x0, top_corpo - hh, x0 + col_w, top_corpo - hh)
            y = top_corpo - hh
            for r in colonna:
                h = r['h']
                c.setStrokeColor(colors.black)
                c.setLineWidth(1)
                bx, by = x0, y - h / 2 - box / 2
                c.rect(bx, by, box, box)
                if r['presente']:
                    # check-in gia' fatto dall'app: casella con la spunta verde
                    c.setStrokeColor(colors.HexColor('#16a34a'))
                    c.setLineWidth(max(1.5, box * 0.2))
                    tick = c.beginPath()
                    tick.moveTo(bx + box * 0.18, by + box * 0.52)
                    tick.lineTo(bx + box * 0.42, by + box * 0.24)
                    tick.lineTo(bx + box * 0.84, by + box * 0.80)
                    c.drawPath(tick, stroke=1, fill=0)
                    c.setLineWidth(1)
                c.setFillColor(colors.black)
                fn = r['fn']
                c.setFont('Helvetica', fn)
                blocco = len(r['r_nome']) * fn * 1.15
                base = y - (h - blocco) / 2 - fn * 0.85
                for k, riga in enumerate(r['r_nome']):
                    c.drawString(x0 + col_arrivo, base - k * fn * 1.15, riga)
                c.setFont('Helvetica', f)
                blocco = len(r['r_posti']) * f * 1.15
                base = y - (h - blocco) / 2 - f * 0.85
                for k, riga in enumerate(r['r_posti']):
                    c.drawString(x0 + col_arrivo + w_nome + sp, base - k * f * 1.15, riga)
                c.setStrokeColor(colors.lightgrey)
                c.line(x0, y - h, x0 + col_w, y - h)
                y -= h


def _genera_pdf_evento(evento):
    posti = (
        Posto.query.filter_by(evento_id=evento.id)
        .options(joinedload(Posto.prenotazione).joinedload(Prenotazione.utente))
        .all()
    )
    posti_by_fc = {(p.fila, p.colonna): p for p in posti}

    prenotazioni = (
        Prenotazione.query.filter_by(evento_id=evento.id)
        .options(joinedload(Prenotazione.utente), joinedload(Prenotazione.posti))
        .order_by(Prenotazione.nome_prenotazione)
        .all()
    )

    corr_col = _parse_corridoi_pdf(evento.corridoio_colonne)
    corr_file = _parse_corridoi_pdf(evento.corridoio_file)

    buffer = io.BytesIO()
    page_size = landscape(A4)
    width, height = page_size
    c = pdfcanvas.Canvas(buffer, pagesize=page_size)

    # Pagina 1: mappa posti (A4 orizzontale)
    titolo = evento.nome
    tf = 16
    while tf > 11 and stringWidth(titolo, 'Helvetica-Bold', tf) > width - 20 * mm:
        tf -= 0.5
    c.setFont('Helvetica-Bold', tf)
    c.drawString(10 * mm, height - 11 * mm, titolo)
    c.setFont('Helvetica', 10)
    intestazione = f"{evento.sala.nome} — {evento.data_evento.strftime('%d/%m/%Y')} {evento.ora_inizio.strftime('%H:%M')}"
    if evento.gestore:
        intestazione += f" — {evento.gestore.ragione_sociale}"
    c.drawString(10 * mm, height - 17 * mm, intestazione)
    c.setFont('Helvetica', 7)
    c.drawString(10 * mm, height - 21 * mm, f"Stampato il {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    margine_sx = 10 * mm
    margine_top = 25 * mm
    area_larghezza = width - 2 * margine_sx
    area_altezza = height - margine_top - 8 * mm

    # i corridoi occupano il 40% di una cella (vedi sotto): cosi' la pianta riempie davvero la pagina
    colonne_visive = evento.colonne + 0.4 * len(corr_col)
    file_visive = evento.file + 0.4 * len(corr_file)
    cella_w = min(34 * mm, area_larghezza / max(colonne_visive, 1))
    cella_h = min(22 * mm, area_altezza / max(file_visive, 1))

    y = height - margine_top
    for f in range(1, evento.file + 1):
        fila_lettera = chr(64 + f)
        x = margine_sx
        for col in range(1, evento.colonne + 1):
            posto = posti_by_fc.get((fila_lettera, col))
            occupato = posto and posto.stato != 'libero' and posto.prenotazione_id
            nome = ''
            if occupato:
                nome = (posto.prenotazione.nome_prenotazione or posto.prenotazione.utente.nome_cognome or '')
            _pdf_disegna_posto(c, x, y, cella_w, cella_h, f"{fila_lettera}{col}", occupato, nome)
            x += cella_w
            if col in corr_col:
                x += cella_w * 0.4
        y -= cella_h
        if f in corr_file:
            y -= cella_h * 0.4

    c.showPage()

    # Pagina 2: elenco prenotazioni (A4 verticale, due colonne)
    _pdf_elenco_a4(c, evento, prenotazioni)

    c.save()
    buffer.seek(0)
    return buffer


@app.route('/admin/evento/<int:event_id>/stampa-pdf')
@login_required
def stampa_evento_pdf(event_id):
    if not current_user.is_admin():
        abort(403)

    evento = db.session.get(Evento, event_id)
    if not evento:
        abort(404)

    buffer = _genera_pdf_evento(evento)
    filename = f"evento_{evento.id}_{evento.data_evento.strftime('%Y%m%d')}.pdf"
    return Response(
        buffer.read(), mimetype='application/pdf',
        headers={'Content-Disposition': f'inline; filename="{filename}"'}
    )


# ==============================================================================
# 11. MIGRAZIONI E STARTUP HOOKS
# ==============================================================================

def _run_startup_migrations():
    try:
        inspector = inspect(db.engine)

        if 'prenotazione' in inspector.get_table_names():
            cols = {c['name'] for c in inspector.get_columns('prenotazione')}
            if 'presente' not in cols:
                db.session.execute(text(
                    "ALTER TABLE prenotazione ADD COLUMN presente BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                app.logger.info('startup-migration: aggiunta prenotazione.presente')
            if 'check_in_at' not in cols:
                db.session.execute(text(
                    "ALTER TABLE prenotazione ADD COLUMN check_in_at TIMESTAMP"
                ))
                app.logger.info('startup-migration: aggiunta prenotazione.check_in_at')
            if 'check_in_da' not in cols:
                db.session.execute(text(
                    "ALTER TABLE prenotazione ADD COLUMN check_in_da INTEGER REFERENCES utente(id)"
                ))
                app.logger.info('startup-migration: aggiunta prenotazione.check_in_da')

        # Gestione Abbonati - Fase 2: colonne nuove (le tabelle nuove le crea db.create_all())
        tabelle = inspector.get_table_names()
        if 'prenotazione' in tabelle and 'categoria_ripetitiva' in tabelle:
            cols = {c['name'] for c in inspector.get_columns('prenotazione')}
            if 'categoria_id' not in cols:
                db.session.execute(text(
                    "ALTER TABLE prenotazione ADD COLUMN categoria_id INTEGER "
                    "REFERENCES categoria_ripetitiva(id) ON DELETE SET NULL"
                ))
                app.logger.info('startup-migration: aggiunta prenotazione.categoria_id')
        if 'posto' in tabelle and 'prenotazioni_ripetitive' in tabelle:
            cols = {c['name'] for c in inspector.get_columns('posto')}
            if 'ripetitiva_id' not in cols:
                db.session.execute(text(
                    "ALTER TABLE posto ADD COLUMN ripetitiva_id INTEGER "
                    "REFERENCES prenotazioni_ripetitive(id) ON DELETE SET NULL"
                ))
                app.logger.info('startup-migration: aggiunta posto.ripetitiva_id')

        db.session.commit()
        _seed_categorie_ripetitive()
        app.logger.info("Migrazioni di avvio completate (o gia' presenti).")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Migrazioni di avvio fallite: {e}')


@app.route('/init-db')
@login_required
def init_db():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)
    db.create_all()
    return 'Database inizializzato!'


@app.route('/admin/migrate-layout-posti')
@login_required
def migrate_layout_posti():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    esiti, aggiunte = [], []
    try:
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        if 'layout_posti' not in tables or 'genere_evento' not in tables:
            return (
                "Le tabelle 'layout_posti' e/o 'genere_evento' non esistono ancora. "
                "Visita <a href='/init-db'>/init-db</a> prima di eseguire questa migrazione."
            ), 400

        sala_columns = [col['name'] for col in inspector.get_columns('sala')]
        if 'overbooking_max' not in sala_columns:
            db.session.execute(text("ALTER TABLE sala ADD COLUMN overbooking_max INTEGER NOT NULL DEFAULT 0"))
            aggiunte.append('sala.overbooking_max')
        else:
            esiti.append("sala.overbooking_max esiste già")

        evento_columns = [col['name'] for col in inspector.get_columns('evento')]
        if 'layout_posti_id' not in evento_columns:
            db.session.execute(text("ALTER TABLE evento ADD COLUMN layout_posti_id INTEGER REFERENCES layout_posti(id)"))
            aggiunte.append('evento.layout_posti_id')
        else:
            esiti.append("evento.layout_posti_id esiste già")

        if 'genere_evento_id' not in evento_columns:
            db.session.execute(text("ALTER TABLE evento ADD COLUMN genere_evento_id INTEGER REFERENCES genere_evento(id)"))
            aggiunte.append('evento.genere_evento_id')
        else:
            esiti.append("evento.genere_evento_id esiste già")

        if 'overbooking_abilitato' not in evento_columns:
            db.session.execute(text("ALTER TABLE evento ADD COLUMN overbooking_abilitato BOOLEAN NOT NULL DEFAULT FALSE"))
            aggiunte.append('evento.overbooking_abilitato')
        else:
            esiti.append("evento.overbooking_abilitato esiste già")

        layout_columns = [col['name'] for col in inspector.get_columns('layout_posti')]
        if 'overbooking_abilitato' not in layout_columns:
            db.session.execute(text("ALTER TABLE layout_posti ADD COLUMN overbooking_abilitato BOOLEAN NOT NULL DEFAULT FALSE"))
            aggiunte.append('layout_posti.overbooking_abilitato')
        else:
            esiti.append("layout_posti.overbooking_abilitato esiste già")

        if aggiunte:
            db.session.commit()
            return (
                "✅ Migrazione completata!<br>"
                f"Colonne aggiunte: {', '.join(aggiunte)}<br>"
                f"{'<br>'.join(esiti)}"
            )
        return "ℹ️ Nessuna migrazione necessaria, colonne già presenti.<br>" + '<br>'.join(esiti)
    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


@app.route('/admin/migrate-google-import')
@login_required
def migrate_google_import():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    esiti, aggiunte = [], []
    try:
        inspector = inspect(db.engine)
        evento_columns = [col['name'] for col in inspector.get_columns('evento')]

        colonne_da_aggiungere = [
            ('origine', "ALTER TABLE evento ADD COLUMN origine VARCHAR(20) NOT NULL DEFAULT 'app'"),
            ('google_event_id', "ALTER TABLE evento ADD COLUMN google_event_id VARCHAR(255)"),
            ('google_calendar_id_origine', "ALTER TABLE evento ADD COLUMN google_calendar_id_origine VARCHAR(255)"),
            ('google_updated', "ALTER TABLE evento ADD COLUMN google_updated TIMESTAMP"),
            ('cancellato_google', "ALTER TABLE evento ADD COLUMN cancellato_google BOOLEAN NOT NULL DEFAULT FALSE"),
        ]

        for nome_colonna, ddl in colonne_da_aggiungere:
            if nome_colonna not in evento_columns:
                db.session.execute(text(ddl))
                aggiunte.append(f'evento.{nome_colonna}')
            else:
                esiti.append(f"evento.{nome_colonna} esiste già")

        indici_esistenti = [idx['name'] for idx in inspector.get_indexes('evento')]
        if 'ix_evento_google_event_id' not in indici_esistenti:
            db.session.execute(text("CREATE INDEX ix_evento_google_event_id ON evento (google_event_id)"))
            aggiunte.append('indice ix_evento_google_event_id')

        if aggiunte:
            db.session.commit()
            return f"✅ Migrazione completata!<br>Aggiunte: {', '.join(aggiunte)}<br>{'<br>'.join(esiti)}"
        return "ℹ️ Nessuna migrazione necessaria.<br>" + '<br>'.join(esiti)
    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


@app.route('/admin/migrate-generi-gestore')
@login_required
def migrate_generi_gestore():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    esiti, aggiunte = [], []
    try:
        inspector = inspect(db.engine)
        colonne_esistenti = [col['name'] for col in inspector.get_columns('genere_evento')]

        colonne_da_aggiungere = [
            ('gestore_id', "ALTER TABLE genere_evento ADD COLUMN gestore_id INTEGER REFERENCES gestore(id)"),
            ('descrizione_aggiuntiva', "ALTER TABLE genere_evento ADD COLUMN descrizione_aggiuntiva TEXT"),
            ('logo', "ALTER TABLE genere_evento ADD COLUMN logo BYTEA"),
            ('logo_mimetype', "ALTER TABLE genere_evento ADD COLUMN logo_mimetype VARCHAR(50)"),
        ]

        for nome_colonna, ddl in colonne_da_aggiungere:
            if nome_colonna not in colonne_esistenti:
                db.session.execute(text(ddl))
                aggiunte.append(f'genere_evento.{nome_colonna}')
            else:
                esiti.append(f"genere_evento.{nome_colonna} esiste già")

        indici_esistenti = [idx['name'] for idx in inspector.get_indexes('genere_evento')]
        if 'ix_genere_evento_gestore_id' not in indici_esistenti:
            db.session.execute(text("CREATE INDEX ix_genere_evento_gestore_id ON genere_evento (gestore_id)"))
            aggiunte.append('indice ix_genere_evento_gestore_id')

        if aggiunte:
            db.session.commit()
            return f"✅ Migrazione completata!<br>Aggiunto: {', '.join(aggiunte)}<br>{'<br>'.join(esiti)}"
        return "ℹ️ Nessuna migrazione necessaria.<br>" + '<br>'.join(esiti)
    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


@app.route('/admin/migrate-prenotazione-checkin')
@login_required
def migrate_prenotazione_checkin():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    esiti, aggiunte = [], []
    try:
        inspector = inspect(db.engine)
        colonne_esistenti = [col['name'] for col in inspector.get_columns('prenotazione')]

        colonne_da_aggiungere = [
            ('presente', "ALTER TABLE prenotazione ADD COLUMN presente BOOLEAN NOT NULL DEFAULT FALSE"),
            ('check_in_at', "ALTER TABLE prenotazione ADD COLUMN check_in_at TIMESTAMP"),
            ('check_in_da', "ALTER TABLE prenotazione ADD COLUMN check_in_da INTEGER REFERENCES utente(id)"),
        ]

        for nome_colonna, ddl in colonne_da_aggiungere:
            if nome_colonna not in colonne_esistenti:
                db.session.execute(text(ddl))
                aggiunte.append(f'prenotazione.{nome_colonna}')
            else:
                esiti.append(f"prenotazione.{nome_colonna} esiste già")

        if aggiunte:
            db.session.commit()
            return f"✅ Migrazione completata!<br>Aggiunto: {', '.join(aggiunte)}<br>{'<br>'.join(esiti)}"
        return "ℹ️ Nessuna migrazione necessaria.<br>" + '<br>'.join(esiti)
    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


@app.route('/admin/migrate-evento-gestore')
@login_required
def migrate_evento_gestore():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    try:
        inspector = inspect(db.engine)
        colonne_esistenti = [col['name'] for col in inspector.get_columns('evento')]

        if 'gestore_id' not in colonne_esistenti:
            db.session.execute(text("ALTER TABLE evento ADD COLUMN gestore_id INTEGER REFERENCES gestore(id)"))
            indici_esistenti = [idx['name'] for idx in inspector.get_indexes('evento')]
            if 'ix_evento_gestore_id' not in indici_esistenti:
                db.session.execute(text("CREATE INDEX ix_evento_gestore_id ON evento (gestore_id)"))
            db.session.commit()
            return "✅ Migrazione completata!<br>Aggiunto: evento.gestore_id (+ indice)"
        return "ℹ️ Nessuna migrazione necessaria, evento.gestore_id esiste già."
    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


@app.route('/admin/migrate-sala-gestore')
@login_required
def migrate_sala_gestore():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    try:
        inspector = inspect(db.engine)
        colonne_esistenti = [col['name'] for col in inspector.get_columns('sala')]

        if 'gestore_default_id' not in colonne_esistenti:
            db.session.execute(text("ALTER TABLE sala ADD COLUMN gestore_default_id INTEGER REFERENCES gestore(id)"))
            db.session.commit()
            return "✅ Migrazione completata!<br>Aggiunto: sala.gestore_default_id"
        return "ℹ️ Nessuna migrazione necessaria, sala.gestore_default_id esiste già."
    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


# Esecuzione automatica creazioni e migrazioni all'avvio del worker
with app.app_context():
    db.create_all()
    _run_startup_migrations()


if __name__ == '__main__':
    app.run()
