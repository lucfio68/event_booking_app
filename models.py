# models.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class Utente(db.Model, UserMixin):
    __tablename__ = 'utente'
    id = db.Column(db.Integer, primary_key=True)
    nome_cognome = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    cellulare = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    tipo = db.Column(db.String(20), default='user')  # admin, user
    data_registrazione = db.Column(db.DateTime, default=datetime.utcnow)

    prenotazioni = db.relationship('Prenotazione', backref='utente', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_admin(self):
        return self.tipo == 'admin'

class Sala(db.Model):
    __tablename__ = 'sala'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    descrizione = db.Column(db.Text, nullable=True)
    indirizzo = db.Column(db.String(255), nullable=True)
    posti_max = db.Column(db.Integer, nullable=False)
    overbooking_max = db.Column(db.Integer, nullable=False, default=0)  # posti extra consentiti oltre posti_max (0 = nessun overbooking permesso)
    email_admin = db.Column(db.Text, nullable=True)

    eventi = db.relationship('Evento', backref='sala', lazy=True, cascade='all, delete-orphan')
    posti = db.relationship('Posto', backref='sala', lazy=True, cascade='all, delete-orphan')

class GenereEvento(db.Model):
    __tablename__ = 'genere_evento'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)  # es. "Concerto", "Conferenza", "Teatro"
    descrizione = db.Column(db.Text, nullable=True)

    layout = db.relationship('LayoutPosti', backref='genere_evento', lazy=True)

class LayoutPosti(db.Model):
    __tablename__ = 'layout_posti'
    id = db.Column(db.Integer, primary_key=True)
    sala_id = db.Column(db.Integer, db.ForeignKey('sala.id'), nullable=False, index=True)
    genere_evento_id = db.Column(db.Integer, db.ForeignKey('genere_evento.id'), nullable=True)  # NULL = layout generico per la sala
    nome = db.Column(db.String(100), nullable=False)  # es. "Platea intera", "Solo prime 10 file"
    file = db.Column(db.Integer, nullable=False)
    colonne = db.Column(db.Integer, nullable=False)
    corridoio_colonne = db.Column(db.String(50), nullable=True, default='')
    corridoio_file = db.Column(db.String(50), nullable=True, default='')
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    overbooking_abilitato = db.Column(db.Boolean, default=False, nullable=False)  # se True, questo layout può superare sala.posti_max (fino a +overbooking_max)
    creato_da = db.Column(db.Integer, db.ForeignKey('utente.id'), nullable=True)
    data_creazione = db.Column(db.DateTime, default=datetime.utcnow)

    sala = db.relationship('Sala', backref=db.backref('layout_posti', lazy=True, cascade='all, delete-orphan'))

    @property
    def posti_totali(self):
        return self.file * self.colonne

class Evento(db.Model):
    __tablename__ = 'evento'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    descrizione = db.Column(db.Text, nullable=True)
    data_evento = db.Column(db.Date, nullable=False, index=True)
    ora_inizio = db.Column(db.Time, nullable=False)
    durata = db.Column(db.Integer, nullable=False)
    posti_max = db.Column(db.Integer, nullable=False)
    file = db.Column(db.Integer, nullable=False)
    colonne = db.Column(db.Integer, nullable=False)
    # Corridoi: numeri separati da virgola (es. "3,6" = corridoio dopo colonna 3 e 6)
    corridoio_colonne = db.Column(db.String(50), nullable=True, default='')
    # Corridoi: numeri separati da virgola (es. "5" = corridoio dopo fila 5)
    corridoio_file = db.Column(db.String(50), nullable=True, default='')
    sala_id = db.Column(db.Integer, db.ForeignKey('sala.id'), nullable=False)
    creato_da = db.Column(db.Integer, db.ForeignKey('utente.id'), nullable=False)
    data_creazione = db.Column(db.DateTime, default=datetime.utcnow)
    # Tracciabilità: da quale layout/genere è nato l'evento (i valori restano copiati sopra, invariati anche se il layout cambia in futuro)
    layout_posti_id = db.Column(db.Integer, db.ForeignKey('layout_posti.id'), nullable=True)
    genere_evento_id = db.Column(db.Integer, db.ForeignKey('genere_evento.id'), nullable=True)
    overbooking_abilitato = db.Column(db.Boolean, default=False, nullable=False)  # se True, l'evento può superare sala.posti_max (fino a +overbooking_max); serve anche per abilitare aggiunte future oltre il limite

    posti = db.relationship('Posto', backref='evento', lazy=True, cascade='all, delete-orphan')
    prenotazioni = db.relationship('Prenotazione', backref='evento', lazy=True, cascade='all, delete-orphan')

class Prenotazione(db.Model):
    __tablename__ = 'prenotazione'
    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(db.Integer, db.ForeignKey('evento.id'), nullable=False, index=True)
    utente_id = db.Column(db.Integer, db.ForeignKey('utente.id'), nullable=False, index=True)
    nome_prenotazione = db.Column(db.String(150), nullable=True)
    stato = db.Column(db.String(50), default='confermata')
    data_prenotazione = db.Column(db.DateTime, default=datetime.utcnow)

    posti = db.relationship('Posto', backref='prenotazione', lazy=True)

class Posto(db.Model):
    __tablename__ = 'posto'
    id = db.Column(db.Integer, primary_key=True)
    sala_id = db.Column(db.Integer, db.ForeignKey('sala.id'), nullable=False)
    evento_id = db.Column(db.Integer, db.ForeignKey('evento.id'), nullable=False)
    numero_posto = db.Column(db.Integer, nullable=False)
    fila = db.Column(db.String(10), nullable=False)
    colonna = db.Column(db.Integer, nullable=False)
    stato = db.Column(db.String(20), default='libero', index=True)  # libero, prenotato, riservato, abbonato
    prenotazione_id = db.Column(db.Integer, db.ForeignKey('prenotazione.id'), nullable=True)

    __table_args__ = (
        db.Index('idx_posto_evento_stato', 'evento_id', 'stato'),
        db.Index('idx_posto_prenotazione', 'prenotazione_id'),
        db.Index('idx_posto_evento_fila_colonna', 'evento_id', 'fila', 'colonna'),
    )


class GoogleConnessione(db.Model):
    """
    Fase B - Connessione OAuth a Google Calendar.
    Pensata per un solo admin collegato alla volta (non multi-account):
    la riga più recente rappresenta la connessione attiva.
    """
    __tablename__ = 'google_connessione'
    id = db.Column(db.Integer, primary_key=True)
    utente_id = db.Column(db.Integer, db.ForeignKey('utente.id'), nullable=False)
    email_google = db.Column(db.String(255), nullable=False)  # account Google collegato, solo per mostrarlo in UI
    refresh_token_cifrato = db.Column(db.Text, nullable=False)  # cifrato con Fernet (TOKEN_ENCRYPTION_KEY)
    scopes = db.Column(db.Text, nullable=True)  # scope OAuth concessi, separati da spazio
    data_connessione = db.Column(db.DateTime, default=datetime.utcnow)
    ultimo_utilizzo = db.Column(db.DateTime, nullable=True)

    utente = db.relationship('Utente', backref='google_connessioni')