# event_booking_app

App prenotazione evento/posto a sedere.

Repo: https://github.com/lucfio68/event_booking_app

## Stato del progetto (26 agosto 2026)

**Fase A — Gestione Layout Posti: ✅ COMPLETATA**
- Gestione Sale via UI (`admin_sale.html`)
- Generi Evento (`admin_generi.html`)
- Layout Posti riutilizzabili per sala (`admin_layout_posti.html`)
- Overbooking a due livelli: tetto per sala (`Sala.overbooking_max`) + attivazione esplicita per istanza (`overbooking_abilitato` su `LayoutPosti`/`Evento`)
- Integrazione layout nel modulo di creazione evento (`event_create.html`)
- Modifica layout su eventi già creati (`admin_evento_layout.html`): aggiunta libera file/colonne, rimozione solo ultima fila/colonna se vuota, corridoi sempre manuali
- Guida utente/admin aggiornata e differenziata per ruolo (v1.7)
- Vari fix responsive/PWA/dark-mode (vedi changelog nella guida per il dettaglio completo)

**Fase B — Import/Export Google Calendar: 🔶 IN CORSO**
- **Step 1 — Connessione OAuth Google: ✅ COMPLETATO E TESTATO**
  Collega/scollega un singolo account Google (account Gmail normale, non Workspace). Modello `GoogleConnessione` (una sola connessione attiva alla volta, `refresh_token` cifrato con Fernet). Route: `/admin/google` (stato + test connessione elencando i calendari), `/admin/google/connect`, `/admin/google/callback`, `/admin/google/disconnect`. Template `admin_google.html`.
- **Step 2 — Associazione Sala ↔ Calendario: ✅ IMPLEMENTATO, DA TESTARE**
  Ogni sala ha un unico calendario Google proprio (relazione 1:1, modello `CalendarioGoogle`). L'elenco calendari mostrato in UI viene dal vivo dallo stesso account collegato allo Step 1 (può includere calendari di altri organizzatori se condivisi con quell'account — non serve collegare più account Google). Route: `/admin/google/sale` (elenco + associazione), `/admin/google/sale/<id>/associa`, `/admin/google/sale/<id>/rimuovi`. Template `admin_google_sale.html`.
  Da testare: visitare `/init-db` per creare la nuova tabella `calendario_google`, poi verificare associazione/rimozione da UI.
- **Step 3 — Import manuale con anteprima/diff: ⬜ DA FARE**
- **Step 4 — Export manuale con anteprima: ⬜ DA FARE**

### Decisioni già prese per la Fase B (non rimetterle in discussione da zero)

- **Import**: pilotato manualmente dall'admin con un tasto "Crea eventi da calendario" — **nessuno scheduler, nessun webhook, nessuna sincronizzazione automatica in background**
- **Export**: manuale allo stesso modo, on-demand
- **Entrambi con una schermata di anteprima/diff** prima di applicare qualsiasi modifica: per ogni evento Google Calendar trovato, l'admin sceglie riga per riga cosa fare in caso di duplicati/sovrapposizioni — Importa come nuovo / Ignora / Sostituisci / Aggiorna / Mantieni entrambi
- Gli eventi importati devono diventare `Evento` **prenotabili veri e propri** (non semplici blocchi visivi nel calendario) — usano i **Layout Posti** della Fase A per assegnare la mappa posti, di default quello contrassegnato come `is_default` per la sala/genere
- **Autenticazione**: OAuth 2.0 one-time per un singolo admin (i calendari sono su account Gmail normali, non Google Workspace — quindi niente Service Account con delega di dominio)
- Motivazione di questo approccio "tutto manuale": semplifica moltissimo l'implementazione rispetto a una sync automatica (niente `syncToken`, niente gestione conflitti "silenziosa", niente token refresh in background) — vedi il resto dell'analisi già fatta in chat per il dettaglio del confronto

### Punti ancora aperti, da decidere insieme prima di scrivere codice

- **Fusi orari**: gli eventi Google hanno timezone esplicito (o sono "all-day" senza orario), mentre `Evento.data_evento`/`ora_inizio` sono "naive" (senza fuso). Da decidere: fuso di riferimento fisso (es. Europe/Rome) e cosa fare con gli eventi "all-day" (escluderli dall'import? chiedere un orario in fase di anteprima?)
- **Eventi ricorrenti**: un evento Google con RRULE può generare decine di istanze. Proposta di partenza: escluderli dall'anteprima import in questa prima versione, o mostrare solo l'occorrenza più vicina — da confermare

### Approccio di lavoro (mantenere anche in Fase B)

Procedere **uno step alla volta**, con un test di conferma dopo ogni step prima di passare al successivo — è l'approccio seguito per tutta la Fase A e ha funzionato bene per individuare subito eventuali problemi.

### Decisioni già prese per la Fase B (da rispettare, non da rimettere in discussione)

- **Import**: pilotato manualmente dall'admin con un tasto "Crea eventi da calendario" — **nessuno** scheduler/cron/webhook/sync automatico
- **Export**: manuale allo stesso modo, on-demand
- **Entrambi con schermata di anteprima/diff** prima di applicare qualsiasi modifica: per ogni evento Google trovato, l'admin sceglie riga per riga l'azione in caso di duplicati/sovrapposizioni — Importa come nuovo / Ignora / Sostituisci / Aggiorna / Mantieni entrambi (coesistenza)
- Gli eventi importati diventano **`Evento` prenotabili veri e propri** (non semplici blocchi visivi nel calendario), usando i **Layout Posti** della Fase A per la mappa posti — di default quello contrassegnato come predefinito per la sala/genere, modificabile riga per riga in anteprima
- **Autenticazione**: OAuth 2.0 one-time per un singolo admin (account Gmail normale, non Google Workspace — quindi niente Service Account con delega di dominio)
- **Modello dati** (aggiornato con l'implementazione reale di Step 1/2):
  - `GoogleConnessione` (utente_id, email_google, refresh_token_cifrato, scopes, data_connessione, ultimo_utilizzo) — una sola riga attiva alla volta
  - `CalendarioGoogle` (sala_id **univoco**, google_calendar_id, nome_calendario, attivo, data_associazione) — una sala ha un unico calendario proprio, usato come destinazione per l'export (Step 4)
  - Ancora da aggiungere in Step 3: `Evento.google_event_id` e `Evento.origine` ('app'|'google') per tracciabilità import
- **Un solo account Google collegato** (non multi-account): quell'account però può vedere più calendari, anche di altri organizzatori se condivisi con lui — è così che si ottengono "calendari diversi tra cui scegliere" senza dover ricollegare account diversi. Se in futuro serve un calendario di un altro account, va prima condiviso con l'account collegato all'app.
- **Import (Step 3) — dettaglio del flusso** confermato in chat: per ogni import l'admin sceglie un calendario SORGENTE tra tutti quelli visibili dall'account collegato (non necessariamente quello associato alla sala), un range di date "oggi + nn giorni", e per ogni evento Google trovato nel periodo decide se caricarlo sulla sala di destinazione o skippare. Deve anche gestire gli aggiornamenti successivi su eventi già importati in precedenza (nuovi inserimenti, cancellazioni lato Google, variazioni di orario) — è lo scopo della schermata anteprima/diff con le azioni Importa/Ignora/Sostituisci/Aggiorna/Mantieni entrambi.

### Punti ancora aperti, da decidere insieme prima di scrivere codice (Step 3)

- **Fusi orari**: gli eventi Google hanno timezone esplicito (o sono "all-day" senza orario), mentre `Evento.data_evento`/`ora_inizio` sono "naive" (senza fuso) — serve decidere un fuso fisso di riferimento (probabilmente Europe/Rome) e cosa fare con gli eventi all-day
- **Eventi ricorrenti** (RRULE): probabile scelta di escluderli/filtrarli nella prima versione, per non generare decine di righe in anteprima da un singolo evento Google
- **Range di date per l'import**: da decidere se un numero fisso configurato una volta oppure un campo che l'admin compila ad ogni import (non ancora deciso)
- **Gestione cancellazioni**: come rilevare e proporre in anteprima un evento importato in precedenza che non esiste più sul calendario Google sorgente (nuovo caso emerso, da progettare insieme allo Step 3)

### Approccio di lavoro da mantenere

Procedere **uno step alla volta**, con un test di conferma dopo ogni step prima di passare al successivo — è l'approccio seguito per tutta la Fase A e ha funzionato bene per individuare rapidamente eventuali problemi.

## Struttura del progetto

```
event_booking_app/
├── app.py                       # Route Flask (incluse tutte le route Fase A)
├── models.py                    # Sala, Evento, Prenotazione, Posto, Utente, GenereEvento, LayoutPosti
├── config.py                    # Configurazione (include MIGRATION_SECRET)
├── requirements.txt
├── migrate_layout_posti.py      # Script di migrazione standalone (alternativa alla route)
├── guida_event_booking.html     # Guida utente/admin v1.7, differenziata per ruolo
├── static/
│   ├── css/style.css
│   ├── js/
│   │   ├── main.js
│   │   ├── calendar.js          # Aggiornato: sync data selezionata col tasto navbar
│   │   ├── booking.js
│   │   ├── admin.js
│   │   └── service-worker.js    # Spostato qui da static/ diretto, cache v1.4
│   ├── manifest.json
│   └── icons/
└── templates/
    ├── base.html                # Navbar dinamica (Calendario ↔ +Crea Evento), color-scheme light
    ├── calendar.html
    ├── event_create.html        # Con integrazione Layout Posti/Generi/Overbooking
    ├── booking.html
    ├── admin_view.html          # Con link a Modifica Layout e fix nome prenotazione
    ├── admin_evento_layout.html # NUOVO — modifica layout evento esistente
    ├── admin_sale.html          # NUOVO — CRUD Sale
    ├── admin_generi.html        # NUOVO — CRUD Generi Evento
    ├── admin_layout_posti.html  # NUOVO — CRUD Layout Posti
    ├── admin_google.html        # NUOVO (Fase B, Step 1) — stato connessione OAuth, test elenco calendari
    ├── admin_google_sale.html   # NUOVO (Fase B, Step 2) — associazione Sala ↔ Calendario Google
    ├── login.html / register.html / forgot_password.html / reset_password_form.html
    └── guida_event_booking.html
```

## ⚠️ Punto aperto non risolto

In una fase di test è comparso questo errore, mai diagnosticato con certezza:
```
werkzeug.routing.exceptions.BuildError: Could not build url for endpoint 'main.calendar_view'.
Did you mean 'calendar_view' instead?
```
Il riferimento a `main.calendar_view` (con prefisso Blueprint) **non è presente in nessuno dei file di questo pacchetto** — probabilmente proveniva da un file non condiviso (un vecchio `wsgi.py`? un template più datato ancora in uso sul server?). Se il problema si ripresenta, cercare `main.` in tutto il repository reale sul server, non solo nei file qui allegati.

## File NON toccati dalla Fase A (versione originale)

`booking.html`, `booking.js`, `calendar.html`, `admin.js`, `main.js`, `login.html`, `register.html`, `forgot_password.html`, `reset_password_form.html` — questi restano quelli originali del progetto, non sono nel pacchetto scaricabile qui allegato (solo i file modificati/nuovi lo sono).

## Link utili ai file (per condivisione rapida con Claude)

Quando vuoi che Claude legga un file specifico, incolla il link diretto tipo:

```
https://github.com/lucfio68/event_booking_app/blob/main/app.py
```

oppure la versione raw (contenuto grezzo):

```
https://raw.githubusercontent.com/lucfio68/event_booking_app/main/app.py
```

## Note

- Ultimo aggiornamento struttura: 26 agosto 2026
- Branch principale: `main`
- Versione guida: v1.7
