ile	Ruolo	Ultima modifica
app.py	Route Flask, logica applicativa	CRUD Sale/Generi/Layout, migrazione
models.py	Schema SQLAlchemy	GenereEvento, LayoutPosti, overbooking
config.py	Configurazione/env vars	MIGRATION_SECRET
base.html	Template padre (navbar, PWA meta)	Nav Calendario, color-scheme, SW path
admin_sale.html	CRUD Sale	Nuovo
admin_generi.html	CRUD Generi Evento	Nuovo
admin_layout_posti.html	CRUD Layout Posti	Nuovo
admin_view.html	Vista "Gestisci" evento	Fix nome prenotazione
style.css	Stili globali	Fix responsive/dark-mode/calendario
manifest.json	Config PWA	Fix splash screen
static/js/service-worker.js	Service Worker PWA	Spostato, v1.4, precache ridotto
migrate_layout_posti.py	Script migrazione offline (alternativa alla route)	Allineato con la route
guida_event_booking.html	Guida utente/admin	v1.5
booking.html, booking.js, calendar.html, calendar.js, admin.js, main.js	Flusso prenotazione/calendario	Non ancora toccati dalla Fase A/B
event_create.html	Form creazione evento	Non ancora toccato — prossimo (Step 4)

Se in una nuova conversazione riprendi da qui, questi sono i file minimi da ricaricarmi per continuare senza perdere contesto: tutti quelli sopra, più eventualmente event_create.html se non l'hai ancora condiviso nella sua ultima versione.

🧭 Analisi funzionale sintetica (per refactoring futuro)

Cosa fa l'app, in breve: sistema di prenotazione posti a sedere per eventi in sale fisiche, con ruoli Utente/Admin, calendario mensile, mappa posti interattiva, PWA installabile.

Moduli funzionali principali:

Autenticazione (login/register/forgot-password/reset-password) — Flask-Login, username o email, recupero password via email con token temporaneo
Calendario (calendar.html/js, /api/events) — vista mensile, doppio click su un giorno mostra eventi, un solo punto di ingresso per creare eventi (ora solo da qui, come appena richiesto)
Creazione evento (event_create.html, /event/create) — form con sala, file/colonne/corridoi inseriti manualmente (ancora non collegato ai Layout Posti nuovi)
Prenotazione (booking.html/js, /api/seats, /api/book) — mappa posti cliccabile, stati (libero/prenotato/riservato/abbonato/mio), nome personalizzabile per prenotazione
Amministrazione evento (admin_view.html, /admin/event/<id>) — vista posti con nomi prenotazione, ricerca, dettagli prenotazione, eliminazione evento
Gestione anagrafica (nuovo, Fase A) — Sale, Generi Evento, Layout Posti — CRUD indipendenti, non ancora collegati al flusso di creazione evento
PWA/infrastruttura — manifest, Service Worker (cache statici), guida integrata con differenziazione per ruolo
Punti di attenzione per un refactoring più ampio:

Duplicazione del renderer posti: esistono tre implementazioni separate della stessa logica di disegno griglia posti (admin.js, booking.js, e main.js con window.SeatRenderer che nessuno dei due primi due usa ancora) — prima occasione utile di refactoring, indipendente da Google Calendar
Evento denormalizza sempre file/colonne/corridoi anche quando nasce da un LayoutPosti — scelta intenzionale (tracciabilità storica), da mantenere anche nei prossimi step
Nessun vincolo DB per l'unicità di is_default su LayoutPosti (gestito in Python) — pattern già usato altrove nell'app (es. tipo utente), coerente ma da tenere a mente se si migra a un ORM/validazione più strict in futuro
Cascade delete automatico su Sala.eventi — pericoloso se non protetto esplicitamente (l'ho bloccato manualmente nel CRUD Sale, ma è un pattern da replicare ogni volta che si aggiunge un punto di eliminazione)