#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crea un utente amministratore per EventBooking.
Esegui dalla root del progetto con l'ambiente virtuale attivo:
    python create_admin.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app
from models import db, Utente

def create_admin():
    print("=" * 50)
    print("  EventBooking - Creazione Admin")
    print("=" * 50)
    print()

    with app.app_context():
        # Chiedi i dati
        nome = input("Nome e Cognome: ").strip()
        username = input("Username (solo lettere/numeri, min 3): ").strip().lower()
        email = input("Email: ").strip().lower()
        password = input("Password (min 8 caratteri): ").strip()
        cellulare = input("Cellulare (opzionale, invio per saltare): ").strip() or None

        # Validazioni
        if not nome or not username or not email or not password:
            print("\n❌ ERRORE: tutti i campi obbligatori devono essere compilati.")
            sys.exit(1)
        if len(password) < 8:
            print("\n❌ ERRORE: la password deve essere di almeno 8 caratteri.")
            sys.exit(1)
        if len(username) < 3 or not username.isalnum():
            print("\n❌ ERRORE: username di almeno 3 caratteri, solo lettere e numeri.")
            sys.exit(1)
        if "@" not in email or "." not in email.split("@")[-1]:
            print("\n❌ ERRORE: inserisci un indirizzo email valido.")
            sys.exit(1)

        # Verifica esistenza
        if Utente.query.filter_by(email=email).first():
            print(f"\n⚠️  L'email '{email}' è già registrata.")
            risposta = input("Vuoi promuoverla a admin? (s/n): ").strip().lower()
            if risposta in ("s", "si", "yes", "y"):
                user = Utente.query.filter_by(email=email).first()
                user.tipo = "admin"
                db.session.commit()
                print(f"\n✅ Utente '{user.nome_cognome}' promosso ad admin!")
            else:
                print("Operazione annullata.")
            return

        if Utente.query.filter_by(username=username).first():
            print(f"\n❌ ERRORE: lo username '{username}' è già in uso.")
            sys.exit(1)

        # Crea admin
        admin = Utente(
            nome_cognome=nome,
            username=username,
            email=email,
            cellulare=cellulare,
            tipo="admin"
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        print("\n" + "=" * 50)
        print("✅ ADMIN CREATO CON SUCCESSO!")
        print("=" * 50)
        print(f"  Nome:     {admin.nome_cognome}")
        print(f"  Username: {admin.username}")
        print(f"  Email:    {admin.email}")
        print(f"  Tipo:     {admin.tipo}")
        print("\nPuoi ora effettuare il login.")

if __name__ == "__main__":
    create_admin()
