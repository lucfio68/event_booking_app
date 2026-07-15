import os
import re
import threading
import socket
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from itsdangerous import URLSafeTimedSerializer
from models import db, Utente, Sala, Evento, Prenotazione, Posto
from config import Config

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
    """Estrae l'indirizzo email da stringhe tipo 'Nome <email@dom.com>'."""
    if not raw:
        return 'noreply@eventbooking.com'
    m = re.search(r'<([^>]+)>', raw)
    return m.group(1).strip() if m else raw.strip()

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

def send_email_message(msg):
    """Invia email via SMTP; se la rete è bloccata (Render free), fallback su Resend API.
    Resend usa HTTPS porta 443, piano free perpetuo, compatibile Render free tier."""
    # Tentativo 1: SMTP (locale / server dedicato)
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5)
    try:
        mail.send(msg)
        socket.setdefaulttimeout(old_timeout)
        return True
    except Exception as smtp_err:
        socket.setdefaulttimeout(old_timeout)
        if not _is_network_error(smtp_err):
            raise  # Errore reale del codice, non di rete

        # Tentativo 2: Resend API (HTTP/443, piano free perpetuo, compatibile Render free tier)
        api_key = app.config.get('RESEND_API_KEY')
        if not api_key:
            raise EmailNetworkError(f'SMTP fallito per rete e RESEND_API_KEY mancante: {smtp_err}')

        try:
            import requests
            payload = {
                "from": _extract_email(msg.sender or app.config.get('MAIL_DEFAULT_SENDER')),
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
        except EmailNetworkError:
            raise
        except Exception as res_err:
            raise EmailNetworkError(f'SMTP: {smtp_err} | Resend: {res_err}')



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
        Evento.data_evento < end_date
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

        posti_max = file * colonne
        if posti_max > sala.posti_max:
            flash(f"I posti creati ({posti_max}) superano la capacita' della sala ({sala.posti_max}).", 'danger')
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
            sala_id=sala_id, creato_da=current_user.id
        )
        db.session.add(evento)
        db.session.flush()

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

    sale = Sala.query.all()
    return render_template('event_create.html', sale=sale, today=date.today().strftime('%Y-%m-%d'))

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
    db.create_all()
    return 'Database inizializzato!'

if __name__ == '__main__':
    app.run()
