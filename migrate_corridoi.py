#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script di migrazione per aggiungere colonne corridoi alla tabella evento.
Esegui dalla root del progetto (dove si trova app.py).

Uso:
    python migrate_corridoi.py

Richiede che l'app Flask sia configurata correttamente con le variabili d'ambiente.
"""

import os
import sys

# Aggiungi la root del progetto al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db
from sqlalchemy import text, inspect

def migrate():
    with app.app_context():
        try:
            # Verifica se le colonne esistono già
            inspector = inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('evento')]

            print("Colonne attuali nella tabella 'evento':")
            print(", ".join(columns))
            print()

            added = []

            if 'corridoio_colonne' not in columns:
                print("Aggiungo colonna 'corridoio_colonne'...")
                db.session.execute(text("ALTER TABLE evento ADD COLUMN corridoio_colonne VARCHAR(50) DEFAULT ''"))
                added.append('corridoio_colonne')
            else:
                print("Colonna 'corridoio_colonne' esiste già.")

            if 'corridoio_file' not in columns:
                print("Aggiungo colonna 'corridoio_file'...")
                db.session.execute(text("ALTER TABLE evento ADD COLUMN corridoio_file VARCHAR(50) DEFAULT ''"))
                added.append('corridoio_file')
            else:
                print("Colonna 'corridoio_file' esiste già.")

            if added:
                db.session.commit()
                print()
                print("=" * 60)
                print("✅ MIGRAZIONE COMPLETATA!")
                print("=" * 60)
                print(f"Colonne aggiunte: {', '.join(added)}")
                print()
                print("Ora puoi creare eventi con corridoi.")
            else:
                print()
                print("=" * 60)
                print("ℹ️  NESSUNA MIGRAZIONE NECESSARIA")
                print("=" * 60)
                print("Le colonne esistono già.")

        except Exception as e:
            db.session.rollback()
            print()
            print("=" * 60)
            print("❌ ERRORE DURANTE LA MIGRAZIONE")
            print("=" * 60)
            print(f"Dettaglio: {str(e)}")
            print()
            print("Possibili cause:")
            print("  - Il database non è raggiungibile")
            print("  - La tabella 'evento' non esiste ancora (esegui prima /init-db)")
            print("  - Permessi insufficienti sul database")
            sys.exit(1)

if __name__ == '__main__':
    migrate()
