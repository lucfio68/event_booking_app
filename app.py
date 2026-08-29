import os
import re
import threading
import socket
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort, session, Response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import func, or_, text, inspect
from sqlalchemy.orm import joinedload
from itsdangerous import URLSafeTimedSerializer
from models import db, Utente, Sala, Evento, Prenotazione, Posto, GenereEvento, LayoutPosti, GoogleConnessione, CalendarioGoogle, Gestore
from config import Config

# Fase B - Google Calendar
import requests as http_requests
from cryptography.fernet import Fernet, InvalidToken
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as google_build
from googleapiclient.errors import HttpError as GoogleHttpError

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

# ==================== EMAIL FALLBACK (SMTP → Resend API) ====================

def _extract_email(raw):
    """Estrae l'indirizzo email da stringhe tipo 'Nome <email@dom.com>'.
    Restituisce None se il formato non è valido."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    m = re.search(r'<([^>]+)>', raw)
    email = m.group(1).strip() if m else raw
    # Validazione base email
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

class EmailNetworkError(Exception):
    pass

def _send_via_brevo(msg, api_key):
    """Invia email tramite l'API HTTP di Brevo. Richiede solo un mittente
    verificato (no dominio DNS obbligatorio), compatibile con Render free tier."""
    import requests
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

    resp = requests.post(
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
    """Invia email tramite l'API HTTP di Resend. Richiede dominio verificato
    per inviare a destinatari diversi dal proprio account."""
    import requests
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

    resp = requests.post(
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
    """Invia email: SMTP -> Brevo (primario per Render free tier) -> Resend (fallback,
    utile quando avrai un dominio verificato su Resend)."""
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

# ==================== UTILITIES ====================

def get_reset_token(email):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='password-reset-salt')

def verify_reset_token(token, max_age=3600):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=max_age)
        return email
    except Exception:
        return None

# ==================== EMAIL WRAPPER (graceful per Render free tier) ====================

_email_queue = []  # Queue in memoria per retry al prossimo avvio

class EmailNetworkError(Exception):
    pass

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

def _graceful_send_email(app, fn, *args, **kwargs):
    """Wrapper che cattura errori di rete senza riempire i log di ERROR."""
    with app.app_context():
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5)
        try:
            fn(*args, **kwargs)
            app.logger.info(f'Email inviata correttamente: {fn.__name__}')
        except Exception as e:
            if _is_network_error(e):
                app.logger.info(f'Email non inviata (rete non disponibile, tipico Render free tier): {fn.__name__} — {e}')
                # Salva in queue per possibile retry
                _email_queue.append({'fn': fn.__name__, 'args': args, 'kwargs': kwargs, 'error': str(e)})
            else:
                app.logger.error(f'Errore invio email async: {e}')
        finally:
            socket.setdefaulttimeout(old_timeout)
            db.session.remove()

def run_email_task(app, fn, *args, **kwargs):
    """Esegue l'invio email in un thread separato con timeout socket ridotto.
    Render free tier blocca SMTP (porta 587), quindi il thread morira' dopo 5s
    invece di bloccare il worker Gunicorn per 30s. Gli errori di rete sono
    catturati gracefulmente e loggati come INFO."""
    thread = threading.Thread(target=_graceful_send_email, args=(app, fn) + args, kwargs=kwargs, daemon=True)
    thread.start()

# ==================== EMAIL FUNCTIONS (chiamate solo da thread) ====================

def _send_registration_email(utente_id):
    """Chiamata solo dal thread di background."""
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
    """Chiamata solo dal thread di background."""
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
    """Chiamata solo dal thread di background."""
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
    """Chiamata solo dal thread di background."""
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
                msg = Message(
                    subject=f'Prenotazione Annullata - {num_posti} {posti_label} - {evento.nome}',
                    recipients=[utente.email],
                    sender='EventBooking <noreply@event_booking.com>',
                    body=f"""Ciao {display_name},

La tua prenotazione per l'evento "{evento.nome}" e' stata annullata (tutti i posti rimossi).

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti annullati ({num_posti}): {posti_str}

Se non hai richiesto tu questa operazione, contatta l'amministratore.
"""
                )
            else:
                msg = Message(
                    subject=f'Posti Annullati - {num_posti} {posti_label} - {evento.nome}',
                    recipients=[utente.email],
                    sender='EventBooking <noreply@event_booking.com>',
                    body=f"""Ciao {display_name},

I posti {posti_str} per l'evento "{evento.nome}" sono stati annullati.

Data: {evento.data_evento.strftime('%d/%m/%Y')}
Ora: {evento.ora_inizio.strftime('%H:%M')}
Sala: {evento.sala.nome}
Posti annullati ({num_posti}): {posti_str}

Se non hai richiesto tu questa operazione, contatta l'amministratore.
"""
                )
            send_email_message(msg)
        except Exception as e:
            app.logger.error(f'Errore email cancellazione: {e}')

def _send_reset_password_email(utente_id, reset_url):
    """Chiamata solo dal thread di background."""
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
    """Chiamata solo dal thread di background. Invia tutte le email di cancellazione."""
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

# ==================== AUTH ====================

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

        # Email in background (non blocca la risposta)
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

        # Email in background (non blocca la risposta)
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

# ==================== CALENDARIO ====================

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


# ==================== GESTIONE EVENTI (ADMIN) ====================

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

        # Se è stato scelto un layout salvato, verifica che appartenga davvero a questa sala
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
            overbooking_abilitato=overbooking_abilitato
        )
        db.session.add(evento)
        db.session.flush()

        # Se non è stato scelto un layout esistente e l'admin ha indicato un nome,
        # salva questa griglia come nuovo layout riutilizzabile per questa sala.
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
    layouts = LayoutPosti.query.all()
    real_today = date.today().strftime('%Y-%m-%d')
    selected_date = request.args.get('date', '')
    return render_template(
        'event_create.html', sale=sale, generi=generi, layouts=layouts,
        today=real_today, selected_date=selected_date
    )


# ==================== MODIFICA LAYOUT EVENTO ESISTENTE (Fase A - rifinitura) ====================
#
# Scope deliberatamente ristretto per sicurezza:
#   - Aggiunta file/colonne: sempre libera, fino al limite sala (+ overbooking se abilitato)
#   - Rimozione: SOLO dell'ultima fila o dell'ultima colonna (mai una posizione intermedia),
#     e solo se completamente libera. Questo evita di dover rinumerare file/colonne successive
#     e di dover decidere come "spezzare" i corridoi già configurati.
#   - Corridoi: mai ricalcolati automaticamente, l'admin li modifica sempre manualmente.

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
        # Lock delle righe coinvolte per evitare che una prenotazione arrivi
        # proprio mentre stiamo verificando/eliminando (race condition).
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

# ==================== ELIMINA EVENTO (ADMIN) ====================

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

# ==================== PRENOTAZIONE ====================

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
    app.logger.warning(f'DEBUG BOOK — Content-Type: {request.content_type!r} | Body: {request.get_data(as_text=True)[:500]!r} | data parsato: {data!r}')
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

    # Email in background (non blocca la risposta HTTP)
    run_email_task(
        app, _send_confirmation_email,
        evento.id, current_user.id,
        [p.id for p in posti],
        nome_prenotazione
    )
    return jsonify({'success': True, 'prenotazione_id': prenotazione.id})

# ==================== RISERVA POSTI (ADMIN) ====================

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


# ==================== ABBONA POSTI (ADMIN) ====================

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

# ==================== ELIMINA SINGOLI POSTI ====================

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

    # Prepara i dati per le email (tutto primitivo, nessun oggetto ORM)
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

    # Invia email in background (non blocca la risposta HTTP)
    run_email_task(app, _send_deletion_emails, email_data_list)

    return jsonify({
        'success': True,
        'posti_eliminati': len(posti),
        'prenotazioni_eliminate': len(prenotazioni_da_eliminare),
        'posti': posti_str_parts
    })

# ==================== ELIMINA PRENOTAZIONE INTERA ====================

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

    # Email in background (non blocca la risposta HTTP)
    run_email_task(
        app, _send_cancellation_email,
        evento.id, utente.id, posti_str,
        True, nome_pren
    )
    return jsonify({'success': True})

# ==================== ADMIN VIEW ====================

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


# ==================== RICERCA POSTI (ADMIN) ====================

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

# ==================== GESTIONE GENERI EVENTO (Fase A) ====================

@app.route('/admin/generi')
@login_required
def admin_generi():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    generi = GenereEvento.query.order_by(GenereEvento.nome).all()
    return render_template('admin_generi.html', generi=generi)


@app.route('/admin/generi/add', methods=['POST'])
@login_required
def admin_generi_add():
    if not current_user.is_admin():
        abort(403)

    nome = request.form.get('nome', '').strip()
    descrizione = request.form.get('descrizione', '').strip()

    if not nome:
        flash('Il nome del genere è obbligatorio.', 'danger')
        return redirect(url_for('admin_generi'))

    if GenereEvento.query.filter(func.lower(GenereEvento.nome) == nome.lower()).first():
        flash(f"Esiste già un genere chiamato '{nome}'.", 'danger')
        return redirect(url_for('admin_generi'))

    genere = GenereEvento(nome=nome, descrizione=descrizione or None)
    db.session.add(genere)
    db.session.commit()
    flash(f"Genere '{nome}' creato.", 'success')
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

    nome = genere.nome
    db.session.delete(genere)
    db.session.commit()
    flash(f"Genere '{nome}' eliminato.", 'success')
    return redirect(url_for('admin_generi'))


# ==================== GESTIONE LAYOUT POSTI (Fase A) ====================

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

    if sala_id:
        sala_selezionata = db.session.get(Sala, sala_id)
        if sala_selezionata:
            layouts = LayoutPosti.query.filter_by(sala_id=sala_id) \
                .order_by(LayoutPosti.is_default.desc(), LayoutPosti.nome).all()

    return render_template(
        'admin_layout_posti.html',
        sale=sale, generi=generi,
        sala_selezionata=sala_selezionata, layouts=layouts
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


def _imposta_layout_default(layout):
    """Resetta il flag is_default sugli altri layout della stessa sala+genere, e lo imposta su questo."""
    altri = LayoutPosti.query.filter(
        LayoutPosti.sala_id == layout.sala_id,
        LayoutPosti.genere_evento_id == layout.genere_evento_id,
        LayoutPosti.id != layout.id
    ).all()
    for altro in altri:
        altro.is_default = False
    layout.is_default = True


# ==================== FASE C - STEP 1: GESTIONE GESTORI (ANAGRAFICA + LOGO) ====================
#
# Anagrafica dell'organizzatore/gestore di eventi. Il logo viene salvato come
# blob nel database (non su filesystem, che su Render è effimero) e servito
# tramite una route dedicata. Nessun collegamento ancora a Sala/GenereEvento
# in questo step (arriva negli step successivi della Fase C).

LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
LOGO_MIMETYPES_AMMESSI = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'}


def _salva_logo_da_form(oggetto, campo_file='logo', campo_rimuovi='rimuovi_logo'):
    """Applica l'eventuale upload/rimozione del logo su un oggetto che ha i
    campi .logo e .logo_mimetype (Gestore, e in futuro GenereEvento). Ritorna
    un messaggio di errore (str) oppure None se tutto ok."""
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

    db.session.delete(gestore)
    db.session.commit()
    flash('Gestore eliminato.', 'success')
    return redirect(url_for('admin_gestori'))


# ==================== GESTIONE SALE ====================

@app.route('/admin/sale')
@login_required
def admin_sale():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    sale = Sala.query.order_by(Sala.nome).all()
    modifica_id = request.args.get('modifica', type=int)
    sala_da_modificare = db.session.get(Sala, modifica_id) if modifica_id else None

    return render_template('admin_sale.html', sale=sale, sala_da_modificare=sala_da_modificare)


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
        email_admin=email_admin or None
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

    # Se riduci posti_max/overbooking_max sotto la capienza di layout/eventi già configurati,
    # non blocchiamo qui (i layout/eventi esistenti restano come sono), ma avvisiamo l'admin.
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
            f"Impossibile eliminare '{sala.nome}': ha {eventi_collegati} eventi collegati "
            f"(eliminandola verrebbero eliminati anche quelli e le relative prenotazioni). "
            f"Elimina prima gli eventi se vuoi comunque procedere.",
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

    nome = sala.nome
    db.session.delete(sala)
    db.session.commit()
    flash(f"Sala '{nome}' eliminata.", 'success')
    return redirect(url_for('admin_sale'))


# ==================== GUIDA ====================

@app.route('/guida')
@login_required
def guida():
    return render_template('guida_event_booking.html')

# ==================== INIT DB ====================

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

# ==================== MIGRAZIONE LAYOUT POSTI (Fase A) ====================

@app.route('/admin/migrate-layout-posti')
@login_required
def migrate_layout_posti():
    if not current_user.is_admin():
        abort(403)
    secret = app.config.get('MIGRATION_SECRET')
    if not secret or request.args.get('key') != secret:
        abort(403)

    esiti = []
    aggiunte = []

    try:
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        if 'layout_posti' not in tables or 'genere_evento' not in tables:
            return (
                "Le tabelle 'layout_posti' e/o 'genere_evento' non esistono ancora. "
                "Visita <a href='/init-db'>/init-db</a> prima di eseguire questa migrazione."
            ), 400

        # === SALA ===
        sala_columns = [col['name'] for col in inspector.get_columns('sala')]
        if 'overbooking_max' not in sala_columns:
            db.session.execute(text(
                "ALTER TABLE sala ADD COLUMN overbooking_max INTEGER NOT NULL DEFAULT 0"
            ))
            aggiunte.append('sala.overbooking_max')
        else:
            esiti.append("sala.overbooking_max esiste già")

        # === EVENTO ===
        evento_columns = [col['name'] for col in inspector.get_columns('evento')]

        if 'layout_posti_id' not in evento_columns:
            db.session.execute(text(
                "ALTER TABLE evento ADD COLUMN layout_posti_id INTEGER REFERENCES layout_posti(id)"
            ))
            aggiunte.append('evento.layout_posti_id')
        else:
            esiti.append("evento.layout_posti_id esiste già")

        if 'genere_evento_id' not in evento_columns:
            db.session.execute(text(
                "ALTER TABLE evento ADD COLUMN genere_evento_id INTEGER REFERENCES genere_evento(id)"
            ))
            aggiunte.append('evento.genere_evento_id')
        else:
            esiti.append("evento.genere_evento_id esiste già")

        if 'overbooking_abilitato' not in evento_columns:
            db.session.execute(text(
                "ALTER TABLE evento ADD COLUMN overbooking_abilitato BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            aggiunte.append('evento.overbooking_abilitato')
        else:
            esiti.append("evento.overbooking_abilitato esiste già")

        # === LAYOUT_POSTI (colonna aggiunta al modello dopo la prima creazione tabella) ===
        layout_columns = [col['name'] for col in inspector.get_columns('layout_posti')]

        if 'overbooking_abilitato' not in layout_columns:
            db.session.execute(text(
                "ALTER TABLE layout_posti ADD COLUMN overbooking_abilitato BOOLEAN NOT NULL DEFAULT FALSE"
            ))
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
        else:
            return "ℹ️ Nessuna migrazione necessaria, colonne già presenti.<br>" + '<br>'.join(esiti)

    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


# ==================== MIGRAZIONE IMPORT GOOGLE (Fase B, Step 3) ====================
# Aggiunge le colonne di tracciabilità import su 'evento' (tabella già esistente:
# db.create_all() non le crea da sola). Stessa protezione di /admin/migrate-layout-posti.

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

        # Indice su google_event_id (usato per il matching negli import successivi)
        indici_esistenti = [idx['name'] for idx in inspector.get_indexes('evento')]
        if 'ix_evento_google_event_id' not in indici_esistenti:
            db.session.execute(text(
                "CREATE INDEX ix_evento_google_event_id ON evento (google_event_id)"
            ))
            aggiunte.append('indice ix_evento_google_event_id')
        else:
            esiti.append("indice ix_evento_google_event_id esiste già")

        if aggiunte:
            db.session.commit()
            return (
                "✅ Migrazione completata!<br>"
                f"Aggiunto: {', '.join(aggiunte)}<br>"
                f"{'<br>'.join(esiti)}"
            )
        else:
            return "ℹ️ Nessuna migrazione necessaria, colonne/indice già presenti.<br>" + '<br>'.join(esiti)

    except Exception as e:
        db.session.rollback()
        return f"❌ Errore durante la migrazione: {str(e)}", 500


# ==================== FASE B - GOOGLE CALENDAR: CONNESSIONE OAUTH ====================
#
# Scope di questo step: SOLO la connessione OAuth (collega/scollega l'account,
# verifica che funzioni elencando i calendari disponibili). Nessuna associazione
# sala<->calendario ancora (arriva nello Step 2), nessun import/export (Step 3/4).

GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
]


def _google_configurato():
    return bool(app.config.get('GOOGLE_CLIENT_ID') and app.config.get('GOOGLE_CLIENT_SECRET')
                and app.config.get('GOOGLE_REDIRECT_URI') and app.config.get('TOKEN_ENCRYPTION_KEY'))


def _google_client_config():
    return {
        "web": {
            "client_id": app.config['GOOGLE_CLIENT_ID'],
            "client_secret": app.config['GOOGLE_CLIENT_SECRET'],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [app.config['GOOGLE_REDIRECT_URI']],
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
    """Restituisce l'unica riga di connessione attiva (la più recente), o None."""
    return GoogleConnessione.query.order_by(GoogleConnessione.data_connessione.desc()).first()


def _credenziali_google(connessione):
    """Costruisce un oggetto Credentials di google-auth a partire dalla connessione salvata,
    decifrando il refresh_token. La libreria lo userà per ottenere un access_token fresco."""
    refresh_token = _decifra_token(connessione.refresh_token_cifrato)
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        scopes=GOOGLE_SCOPES,
    )


@app.route('/admin/google')
@login_required
def admin_google_status():
    if not current_user.is_admin():
        flash('Accesso riservato agli amministratori.', 'danger')
        return redirect(url_for('calendar_view'))

    if not _google_configurato():
        flash(
            "Google Calendar non è ancora configurato sul server: mancano una o più variabili "
            "d'ambiente (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI, TOKEN_ENCRYPTION_KEY).",
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
            errore_calendari = f"Errore durante il test: {e}"

    return render_template(
        'admin_google.html',
        connessione=connessione, calendari=calendari, errore_calendari=errore_calendari,
        google_configurato=_google_configurato()
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
        redirect_uri=app.config['GOOGLE_REDIRECT_URI']
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',      # necessario per ottenere un refresh_token
        prompt='consent',           # forza il consenso ogni volta, garantendo il refresh_token anche su ri-connessioni
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
            redirect_uri=app.config['GOOGLE_REDIRECT_URI']
        )
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        if not creds.refresh_token:
            flash(
                "Google non ha restituito un refresh_token. Prova a scollegare l'account da "
                "https://myaccount.google.com/permissions e ripetere la connessione.", 'danger'
            )
            return redirect(url_for('admin_google_status'))

        # Recupera l'email dell'account collegato
        userinfo = http_requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {creds.token}'}, timeout=10
        ).json()
        email_google = userinfo.get('email', 'sconosciuta')

        # Design a singola connessione attiva: rimuovo eventuali precedenti
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
        # Best-effort: prova a revocare il token lato Google (non blocca in caso di errore)
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


# ==================== FASE B - STEP 2: ASSOCIAZIONE SALA <-> CALENDARIO ====================
#
# Scope di questo step: tabella + UI per associare a ciascuna sala il proprio
# calendario Google (uno solo per sala). L'elenco dei calendari mostrati in UI
# viene dallo stesso account già collegato nello Step 1 (calendarList()), che
# può includere anche calendari di altri organizzatori se condivisi con
# quell'account. Nessun import/export reale in questo step (arriva in Step 3/4).

def _elenca_calendari_google():
    """Ritorna (lista_calendari, errore). lista_calendari è None se non c'è
    una connessione attiva o se la chiamata fallisce."""
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
        return None, f"Errore durante il recupero dei calendari: {e}"


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

    # Il nome lo recuperiamo dall'elenco appena mostrato in pagina (passato come campo nascosto)
    # per non dover richiamare l'API Google solo per il display name.
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


# ==================== FASE B - STEP 3: IMPORT MANUALE CON ANTEPRIMA/DIFF ====================
#
# Scope: l'admin sceglie un calendario SORGENTE (tra tutti quelli visibili
# dall'account Google collegato, non necessariamente quello associato alla
# sala) e un range "oggi + N giorni". Per ogni evento Google trovato nel
# periodo, l'app mostra un'anteprima con lo stato (nuovo/modificato/invariato)
# e l'admin sceglie l'azione riga per riga PRIMA che qualsiasi modifica venga
# applicata al database. Fuso orario di riferimento fisso: Europe/Rome.
# Eventi "intera giornata" non sono supportati in questa versione (compaiono
# in anteprima come non importabili). Le occorrenze di eventi ricorrenti nel
# periodo vengono già espanse da Google stesso (singleEvents=True) e importate
# come eventi singoli indipendenti, senza alcun legame con la ricorrenza.

from zoneinfo import ZoneInfo

FUSO_ORARIO_APP = ZoneInfo('Europe/Rome')


def _parse_datetime_google(valore_iso):
    """Converte una stringa dateTime RFC3339 di Google in un datetime timezone-aware."""
    return datetime.fromisoformat(valore_iso.replace('Z', '+00:00'))


def _layout_default_per_sala(sala_id):
    """Trova il layout da usare per un evento importato: preferisce il default
    'generico' (genere_evento_id NULL), altrimenti un default qualsiasi di
    quella sala. Ritorna None se la sala non ha nessun layout di default."""
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


def _crea_evento_da_import(sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, google_event_id):
    """Crea un nuovo Evento prenotabile a partire da un evento Google importato,
    usando il layout di default della sala scelta. Ritorna (evento, None) oppure
    (None, messaggio_errore) se la sala non ha un layout di default idoneo."""
    layout = _layout_default_per_sala(sala_id)
    if not layout:
        return None, 'nessun layout di default configurato per questa sala'

    sala = db.session.get(Sala, sala_id)
    posti_max = layout.file * layout.colonne
    limite = sala.posti_max + (sala.overbooking_max if layout.overbooking_abilitato else 0)
    if posti_max > limite:
        return None, f'il layout di default ({posti_max} posti) supera la capacità della sala ({limite})'

    evento = Evento(
        nome=nome, descrizione=descrizione, data_evento=data_obj, ora_inizio=ora_obj,
        durata=durata, posti_max=posti_max, file=layout.file, colonne=layout.colonne,
        corridoio_colonne=layout.corridoio_colonne, corridoio_file=layout.corridoio_file,
        sala_id=sala_id, creato_da=current_user.id,
        layout_posti_id=layout.id, genere_evento_id=layout.genere_evento_id,
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
    """Eventi Google nel periodo [ora, ora+giorni]; singleEvents=True fa sì che
    Google stesso espanda le occorrenze di eventi ricorrenti come voci singole."""
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
    """Estrae i campi rilevanti nel fuso Europe/Rome. Ritorna None se l'evento
    è 'intera giornata' (nessun orario), non supportato in questa versione."""
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

    sale = Sala.query.order_by(Sala.nome).all()
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
            errore_import = f"Errore durante il recupero degli eventi: {e}"

    return render_template(
        'admin_google_import.html',
        calendari=calendari, errore_calendari=errore_calendari,
        calendar_id=calendar_id, giorni=giorni, sale=sale,
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
            evento.google_updated = datetime.utcnow()
            contatori['aggiornati'] += 1

        elif azione == 'sostituisci':
            evento_vecchio = db.session.get(Evento, evento_id_esistente) if evento_id_esistente else None
            if evento_vecchio:
                db.session.delete(evento_vecchio)
                db.session.flush()
            _, errore = _crea_evento_da_import(sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, gid)
            if errore:
                contatori['errori'] += 1
                flash(f'Riga "{nome}": {errore}, impossibile creare l\'evento.', 'danger')
                continue
            contatori['sostituiti'] += 1

        elif azione == 'importa_nuovo':
            _, errore = _crea_evento_da_import(sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, gid)
            if errore:
                contatori['errori'] += 1
                flash(f'Riga "{nome}": {errore}, impossibile creare l\'evento.', 'danger')
                continue
            contatori['importati'] += 1

        elif azione == 'mantieni_entrambi':
            # Evento aggiuntivo separato, SENZA google_event_id: l'evento originale
            # resta l'unico collegato a questo id Google nei futuri import.
            _, errore = _crea_evento_da_import(sala_id, nome, descrizione, data_obj, ora_obj, durata, calendar_id, None)
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
    return redirect(url_for('admin_google_import', calendar_id=calendar_id, giorni=giorni))


if __name__ == '__main__':
    app.run()
