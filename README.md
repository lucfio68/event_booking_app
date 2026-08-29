# event_booking_app

App prenotazione evento/posto a sedere.

Repo: https://github.com/lucfio68/event_booking_app

## Stato del progetto (28 agosto 2026)

**Fase A — Gestione Layout Posti: ✅ COMPLETATA**
- Gestione Sale via UI (`admin_sale.html`)
- Generi Evento (`admin_generi.html`)
- Layout Posti riutilizzabili per sala (`admin_layout_posti.html`)
- Overbooking a due livelli: tetto per sala (`Sala.overbooking_max`) + attivazione esplicita per istanza (`overbooking_abilitato` su `LayoutPosti`/`Evento`)
- Integrazione layout nel modulo di creazione evento (`event_create.html`)
- Modifica layout su eventi già creati (`admin_evento_layout.html`): aggiunta libera file/colonne, rimozione solo ultima fila/colonna se vuota, corridoi sempre manuali
- Guida utente/admin aggiornata e differenziata per ruolo (v1.7)
- Vari fix responsive/PWA/dark-mode (vedi changelog nella guida per il dettaglio completo)

**Fase B — Import Google Calendar: ✅ COMPLETATA (Step 4 congelato)**
- **Step 1 — Connessione OAuth Google: ✅ COMPLETATO E TESTATO**
  Collega/scollega un singolo account Google (account Gmail normale, non Workspace). Modello `GoogleConnessione` (una sola connessione attiva alla volta, `refresh_token` cifrato con Fernet). Route: `/admin/google` (stato + test connessione elencando i calendari), `/admin/google/connect`, `/admin/google/callback`, `/admin/google/disconnect`. Template `admin_google.html`.
- **Step 2 — Associazione Sala ↔ Calendario: ✅ IMPLEMENTATO, DA TESTARE**
  Ogni sala ha un unico calendario Google proprio (relazione 1:1, modello `CalendarioGoogle`). L'elenco calendari mostrato in UI viene dal vivo dallo stesso account collegato allo Step 1 (può includere calendari di altri organizzatori se condivisi con quell'account — non serve collegare più account Google). Route: `/admin/google/sale` (elenco + associazione), `/admin/google/sale/<id>/associa`, `/admin/google/sale/<id>/rimuovi`. Template `admin_google_sale.html`.
  Da testare: visitare `/init-db` per creare la nuova tabella `calendario_google`, poi verificare associazione/rimozione da UI.
- **Step 3 — Import manuale con anteprima/diff: ✅ IMPLEMENTATO, DA TESTARE**
  L'admin sceglie un calendario sorgente e un range di giorni; l'anteprima mostra eventi Nuovi / Modificati (con diff campo per campo) / Invariati / Non importabili (eventi "intera giornata") / Rimossi lato Google. Route: `/admin/google/import` (GET, anteprima), `/admin/google/import/applica` (POST, applica le azioni scelte riga per riga). Template `admin_google_import.html`. Nessuna chiamata scrive su Google in questo step (solo lettura, `calendar.readonly`).
  **Da fare prima del primo test**: visitare `/admin/migrate-google-import?key=<MIGRATION_SECRET>` — `Evento` è una tabella già esistente e `db.create_all()` (via `/init-db`) non aggiunge colonne a tabelle già presenti (lo fa solo per tabelle nuove come `calendario_google` in Step 2). Questa route aggiunge le 5 colonne nuove + l'indice su `Evento` con lo stesso pattern già usato in Fase A (`/admin/migrate-layout-posti`).
- **Step 4 — Export manuale con anteprima: ❄️ CONGELATO, non necessario**
  Deciso il 28 agosto 2026: gli eventi restano un flusso a senso unico (Google → app). Non serve esportare verso Google eventi creati nell'app. Le fondamenta (`CalendarioGoogle` come destinazione teorica, campi di tracciabilità su `Evento`) restano comunque a disposizione se in futuro dovesse servire.

**Fase B considerata chiusa** salvo emergano errori durante il test conclusivo di Step 1-3 già consegnati.

### Decisioni già prese per la Fase B (da rispettare, non da rimettere in discussione)

- **Import**: pilotato manualmente dall'admin con un tasto "Crea eventi da calendario" — **nessuno** scheduler/cron/webhook/sync automatico
- **Export**: manuale allo stesso modo, on-demand
- **Entrambi con schermata di anteprima/diff** prima di applicare qualsiasi modifica: per ogni evento Google trovato, l'admin sceglie riga per riga l'azione in caso di duplicati/sovrapposizioni — Importa come nuovo / Ignora / Sostituisci / Aggiorna / Mantieni entrambi (coesistenza)
- Gli eventi importati diventano **`Evento` prenotabili veri e propri** (non semplici blocchi visivi nel calendario), usando i **Layout Posti** della Fase A per la mappa posti — di default quello contrassegnato come predefinito per la sala/genere, modificabile riga per riga in anteprima
- **Autenticazione**: OAuth 2.0 one-time per un singolo admin (account Gmail normale, non Google Workspace — quindi niente Service Account con delega di dominio)
- **Un solo account Google collegato** (non multi-account): quell'account però può vedere più calendari, anche di altri organizzatori se condivisi con lui — è così che si ottengono "calendari diversi tra cui scegliere" senza dover ricollegare account diversi. Se in futuro serve un calendario di un altro account, va prima condiviso con l'account collegato all'app.
- **Modello dati** (aggiornato con l'implementazione reale di Step 1/2/3):
  - `GoogleConnessione` (utente_id, email_google, refresh_token_cifrato, scopes, data_connessione, ultimo_utilizzo) — una sola riga attiva alla volta
  - `CalendarioGoogle` (sala_id **univoco**, google_calendar_id, nome_calendario, attivo, data_associazione) — una sala ha un unico calendario proprio, usato come destinazione per l'export (Step 4)
  - `Evento.origine` ('app'|'google'), `Evento.google_event_id` (indicizzato), `Evento.google_calendar_id_origine`, `Evento.google_updated`, `Evento.cancellato_google` — aggiunti in Step 3 per tracciabilità import
- **Import (Step 3) — flusso implementato**: per ogni import l'admin sceglie un calendario SORGENTE tra tutti quelli visibili dall'account collegato (non necessariamente quello associato alla sala) e un range "oggi + nn giorni" (campo compilato ad ogni import, default 60 giorni). Per ogni evento Google trovato nel periodo, l'anteprima mostra lo stato (nuovo / modificato rispetto a un import precedente / invariato) e l'admin sceglie riga per riga la sala di destinazione e l'azione, prima di applicare qualunque modifica.
- **Fusi orari**: fuso di riferimento fisso **Europe/Rome** (libreria stdlib `zoneinfo` + pacchetto `tzdata` in requirements per portabilità). Gli eventi Google "intera giornata" (senza orario) non sono supportati in questa prima versione: compaiono in anteprima nella sezione "Non importabili".
- **Eventi ricorrenti**: nessuna gestione esplicita della ricorrenza (RRULE). Si usa `singleEvents=True` nella chiamata a Google, che fa espandere a Google stesso le occorrenze nel periodo scelto: ogni occorrenza viene trattata come un evento indipendente, senza legame con la serie.
- **Cancellazioni lato Google**: un evento importato in precedenza (nel periodo scansionato) che non compare più tra i risultati Google viene segnalato in una sezione separata dell'anteprima; l'admin può ignorare, segnarlo come annullato (`cancellato_google=True`: sparisce dal calendario prenotabile ma le prenotazioni esistenti restano in database, utile per contattare i clienti) oppure eliminarlo definitivamente (cascata su posti/prenotazioni).
- **Aggiorna vs Sostituisci** (per un evento già importato in precedenza e poi modificato su Google): "Aggiorna" cambia solo nome/descrizione/data/ora/durata/sala mantenendo intatti posti e prenotazioni esistenti; "Sostituisci" elimina l'evento esistente (con le sue prenotazioni) e lo ricrea da zero col layout di default corrente. "Mantieni entrambi" lascia intatto l'evento esistente e ne crea uno nuovo separato (senza `google_event_id`, per non creare ambiguità nei futuri import).

### Approccio di lavoro da mantenere

Procedere **uno step alla volta**, con un test di conferma dopo ogni step prima di passare al successivo — è l'approccio seguito per tutta la Fase A e la Fase B e ha funzionato bene per individuare rapidamente eventuali problemi.

## Fase C — Stampe e Check-in con QR Code (in analisi, non ancora iniziata)

Richiesta del 28 agosto 2026, da analizzare a step come le fasi precedenti prima di scrivere codice.

**Obiettivo 1 — Stampa A3 mappa posti + elenco prenotazioni**
Un admin deve poter generare un PDF formato A3 con la situazione dei posti prenotati e l'elenco delle prenotazioni di un evento, da usare all'ingresso per indirizzare le persone al proprio posto al momento dell'acquisto/ritiro biglietto.

**Obiettivo 2 — Biglietto virtuale con QR code**
Alla conferma di una prenotazione, inviare (probabilmente via email, riusando `_send_confirmation_email` già esistente) anche un biglietto con QR code. Leggendo il QR con l'app da un telefono con utente admin, o da un totem con lettore dedicato all'ingresso, il sistema deve mostrare le indicazioni per accompagnare la persona al proprio posto.

### Step proposti (da confermare)

- **Step 1 — Generazione PDF A3**: nuova libreria Python per PDF (proposta: `reportlab`, puro Python, senza dipendenze di sistema pesanti — più adatto a un deploy Render rispetto a `weasyprint` che richiede Cairo/Pango). Route admin "Stampa" su un evento: disegna la mappa posti colorata (stessa logica fila/colonna/corridoi già usata per il grid a schermo) più una tabella con nome/posti/stato di ogni prenotazione.
- **Step 2 — Generazione QR code e biglietto**: libreria `qrcode` (pura Python). Il QR non deve contenere l'id della prenotazione in chiaro ma un token firmato (via `itsdangerous`, già una dipendenza del progetto) per evitare che si possa costruire/indovinare il QR di qualcun altro. Il QR punta a un URL del tipo `/admin/checkin/<token>`.
- **Step 3 — Pagina di check-in da telefono admin**: aprendo quell'URL (autenticato come admin) si vede una schermata con nome/evento/sala/posti da comunicare alla persona. Nessuna libreria di scansione in-app necessaria se il QR è semplicemente un URL: la fotocamera nativa del telefono lo apre da sola.
- **Step 4 — Totem con lettore fisico all'ingresso**: pagina "chiosco" a schermo intero pensata per un lettore QR hardware collegato come tastiera (digita il contenuto scansionato + invio in un campo sempre attivo), mostra a caratteri grandi le indicazioni e si resetta per la scansione successiva. Richiede una decisione sull'accesso (vedi punti aperti sotto): non è detto sia sicuro lasciare una sessione admin completa aperta su un dispositivo incustodito all'ingresso.

### Punti aperti da decidere insieme prima di scrivere codice

- **Cosa stampare in A3**: mappa posti soltanto, o anche una seconda pagina con l'elenco nominale ordinato (per cercare più velocemente una persona per cognome)?
- **Quando generare il PDF**: on-demand ogni volta che serve (sempre aggiornato, ma un click in più prima di ogni turno), oppure con un pulsante dedicato "Stampa mappa" nella pagina evento?
- **Un solo QR per prenotazione o un QR per singolo posto**: dato che una prenotazione può includere più posti, il QR identifica l'intera prenotazione (mostrando tutti i posti insieme) o serve un QR distinto per ciascun posto/persona?
- **QR monouso o riusabile**: serve segnare la prenotazione come "check-in effettuato" per evitare che lo stesso biglietto venga riletto più volte (rivendita, ingresso multiplo), oppure la lettura è solo informativa senza tracciare lo stato?
- **Sicurezza del totem**: una sessione admin lasciata aperta su un dispositivo fisico all'ingresso è un rischio (accesso a tutto il pannello admin se rubato/manomesso). Alternative: un token dedicato "solo lettura check-in" separato dal login admin completo, oppure si accetta il rischio assumendo il totem sorvegliato.
- **Hardware del totem**: già disponibile (marca/modello del lettore QR) o ancora da scegliere? Cambia se serve emulazione tastiera (soluzione più semplice, sopra) o integrazione più complessa.

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
    ├── admin_google_import.html # NUOVO (Fase B, Step 3) — import con anteprima/diff
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

## Changelog Fase B (versioning delle modifiche)

- **28 agosto 2026** — Fase B chiusa: Step 4 (Export) congelato su decisione dell'utente, non necessario. Aperta l'analisi della Fase C (Stampe A3 + Check-in QR Code).
- **v1.3.0 (26 agosto 2026)** — Step 3: import manuale con anteprima/diff. Nuove colonne su `Evento` (`origine`, `google_event_id`, `google_calendar_id_origine`, `google_updated`, `cancellato_google`), nuova route di migrazione `/admin/migrate-google-import`, nuovo template `admin_google_import.html`, filtro `cancellato_google` su `/api/events` e blocco prenotazione su eventi annullati in `booking_page`.
- **v1.2.0 (26 agosto 2026)** — Step 2: associazione Sala ↔ Calendario (`CalendarioGoogle`, route `/admin/google/sale*`, template `admin_google_sale.html`).
- **v1.1.0 (data non specificata, dichiarato completato e testato dall'utente)** — Step 1: connessione OAuth Google (`GoogleConnessione`, route `/admin/google*`, template `admin_google.html`).
- **v1.0.0 (2 agosto 2026)** — Fase A completata: Layout Posti, Generi Evento, overbooking a due livelli.

## Note

- Ultimo aggiornamento struttura: 26 agosto 2026
- Branch principale: `main`
- Versione guida: v1.7
- Versione backend (Fase B): v1.3.0 — vedi changelog sopra
