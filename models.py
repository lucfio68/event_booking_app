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
    email_admin = db.Column(db.Text, nullable=True)

    eventi = db.relationship('Evento', backref='sala', lazy=True, cascade='all, delete-orphan')
    posti = db.relationship('Posto', backref='sala', lazy=True, cascade='all, delete-orphan')

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
    sala_id = db.Column(db.Integer, db.ForeignKey('sala.id'), nullable=False)
    creato_da = db.Column(db.Integer, db.ForeignKey('utente.id'), nullable=False)
    data_creazione = db.Column(db.DateTime, default=datetime.utcnow)

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