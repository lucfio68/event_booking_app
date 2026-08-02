#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script di migrazione per la Fase A (Gestione Layout Posti).

Aggiunge:
  - sala.overbooking_max
  - evento.layout_posti_id
  - evento.genere_evento_id
  - evento.overbooking_abilitato

IMPORTANTE: eseguire DOPO aver visitato /init-db, che crea le tabelle
'genere_evento' e 'layout_posti' (tabelle nuove, non serve ALTER per quelle).

Esegui dalla root del progetto (dove si trova app.py).

Uso:
    python migrate_layout_posti.py

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
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()

            # Le tabelle nuove devono già esistere (create da db.create_all() via /init-db)
            if 'layout_posti' not in tables or 'genere_evento' not in tables:
                print("❌ Le tabelle 'layout_posti' e/o 'genere_evento' non esistono ancora.")
                print("   Visita /init-db (come admin) prima di eseguire questo script.")
                sys.exit(1)

            added = []

            # === SALA ===
            sala_columns = [col['name'] for col in inspector.get_columns('sala')]
            print("Colonne attuali nella tabella 'sala':")
            print(", ".join(sala_columns))
            print()

            if 'overbooking_max' not in sala_columns:
                print("Aggiungo colonna 'sala.overbooking_max'...")
                db.session.execute(text(
                    "ALTER TABLE sala ADD COLUMN overbooking_max INTEGER NOT NULL DEFAULT 0"
                ))
                added.append('sala.overbooking_max')
            else:
                print("Colonna 'sala.overbooking_max' esiste già.")

            # === EVENTO ===
            evento_columns = [col['name'] for col in inspector.get_columns('evento')]
            print()
            print("Colonne attuali nella tabella 'evento':")
            print(", ".join(evento_columns))
            print()

            if 'layout_posti_id' not in evento_columns:
                print("Aggiungo colonna 'evento.layout_posti_id'...")
                db.session.execute(text(
                    "ALTER TABLE evento ADD COLUMN layout_posti_id INTEGER REFERENCES layout_posti(id)"
                ))
                added.append('evento.layout_posti_id')
            else:
                print("Colonna 'evento.layout_posti_id' esiste già.")

            if 'genere_evento_id' not in evento_columns:
                print("Aggiungo colonna 'evento.genere_evento_id'...")
                db.session.execute(text(
                    "ALTER TABLE evento ADD COLUMN genere_evento_id INTEGER REFERENCES genere_evento(id)"
                ))
                added.append('evento.genere_evento_id')
            else:
                print("Colonna 'evento.genere_evento_id' esiste già.")

            if 'overbooking_abilitato' not in evento_columns:
                print("Aggiungo colonna 'evento.overbooking_abilitato'...")
                db.session.execute(text(
                    "ALTER TABLE evento ADD COLUMN overbooking_abilitato BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                added.append('evento.overbooking_abilitato')
            else:
                print("Colonna 'evento.overbooking_abilitato' esiste già.")

            # === LAYOUT_POSTI (colonna aggiunta al modello dopo la prima creazione tabella) ===
            layout_columns = [col['name'] for col in inspector.get_columns('layout_posti')]

            if 'overbooking_abilitato' not in layout_columns:
                print("Aggiungo colonna 'layout_posti.overbooking_abilitato'...")
                db.session.execute(text(
                    "ALTER TABLE layout_posti ADD COLUMN overbooking_abilitato BOOLEAN NOT NULL DEFAULT FALSE"
                ))
                added.append('layout_posti.overbooking_abilitato')
            else:
                print("Colonna 'layout_posti.overbooking_abilitato' esiste già.")

            if added:
                db.session.commit()
                print()
                print("=" * 60)
                print("✅ MIGRAZIONE COMPLETATA!")
                print("=" * 60)
                print(f"Colonne aggiunte: {', '.join(added)}")
                print()
                print("Tutti i record esistenti hanno valori di default sicuri:")
                print("  - sala.overbooking_max = 0 (nessun overbooking finché non lo attivi)")
                print("  - evento.overbooking_abilitato = FALSE")
                print("  - evento.layout_posti_id / genere_evento_id = NULL")
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
            print("  - Le tabelle 'sala'/'evento' non esistono ancora (esegui prima /init-db)")
            print("  - Permessi insufficienti sul database")
            sys.exit(1)


if __name__ == '__main__':
    migrate()
