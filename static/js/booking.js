# static/js/booking.js
(function() {
    'use strict';

    function setDebug(msg) {
        var el = document.getElementById('debugInfo');
        if (el) el.textContent = 'DEBUG: ' + msg;
        console.log('[BOOKING]', msg);
    }

    function showMessage(elementId, message, type) {
        type = type || 'info';
        var el = document.getElementById(elementId);
        if (!el) return;
        el.className = 'alert alert-' + type;
        el.textContent = message;
        el.style.display = 'block';
    }

    document.addEventListener('DOMContentLoaded', function() {
        setDebug('DOM caricato');

        var container = document.getElementById('seatsContainer');
        var btnConfirm = document.getElementById('btnConfirm');
        var btnCancel = document.getElementById('btnCancel');

        if (!container) {
            setDebug('ERRORE: #seatsContainer non trovato');
            return;
        }

        if (!window.BOOKING_CONFIG) {
            setDebug('ERRORE: BOOKING_CONFIG mancante');
            container.innerHTML = '<p class="text-danger">Errore configurazione. Ricarica la pagina (Ctrl+F5).</p>';
            return;
        }

        var EVENT_ID = window.BOOKING_CONFIG.eventId;
        var FILE = window.BOOKING_CONFIG.file;
        var COLONNE = window.BOOKING_CONFIG.colonne;

        setDebug('Config: eventId=' + EVENT_ID + ', file=' + FILE + ', colonne=' + COLONNE);

        if (!EVENT_ID || !FILE || !COLONNE) {
            setDebug('ERRORE: dati evento incompleti');
            container.innerHTML = '<p class="text-danger">Dati evento incompleti. Contatta l'amministratore.</p>';
            return;
        }

        var selectedSeats = new Set();
        var seatsData = [];

        function loadSeats() {
            setDebug('Caricamento posti da /api/seats/' + EVENT_ID + '...');

            var controller = new AbortController();
            var timeoutId = setTimeout(function() {
                controller.abort();
                setDebug('TIMEOUT: richiesta troppo lenta');
                container.innerHTML = '<p class="text-danger">Timeout: il server non risponde. Verifica che l'app sia avviata.</p>';
            }, 10000); // 10 secondi timeout

            fetch('/api/seats/' + EVENT_ID, { signal: controller.signal })
                .then(function(resp) {
                    clearTimeout(timeoutId);
                    setDebug('Risposta HTTP: ' + resp.status);

                    if (!resp.ok) {
                        return resp.text().then(function(text) {
                            throw new Error('HTTP ' + resp.status + ': ' + text.substring(0, 200));
                        });
                    }
                    return resp.json();
                })
                .then(function(data) {
                    setDebug('Posti ricevuti: ' + data.length);
                    seatsData = data;
                    renderSeats();
                })
                .catch(function(e) {
                    clearTimeout(timeoutId);
                    setDebug('ERRORE: ' + e.message);
                    console.error(e);
                    container.innerHTML = '<p class="text-danger">Errore caricamento: ' + e.message + '</p>' +
                        '<p style="font-size:0.85em;color:#7f8c8d;">Prova a ricaricare con Ctrl+F5</p>';
                });
        }

        function renderSeats() {
            container.innerHTML = '';

            // Header colonne
            var headerRow = document.createElement('div');
            headerRow.className = 'seat-row';
            headerRow.innerHTML = '<div class="row-label"></div>';
            for (var c = 1; c <= COLONNE; c++) {
                headerRow.innerHTML += '<div class="seat col-label">' + c + '</div>';
            }
            container.appendChild(headerRow);

            if (seatsData.length === 0) {
                var emptyMsg = document.createElement('div');
                emptyMsg.style.padding = '20px';
                emptyMsg.style.textAlign = 'center';
                emptyMsg.style.color = '#7f8c8d';
                emptyMsg.textContent = 'Nessun posto disponibile per questo evento.';
                container.appendChild(emptyMsg);
                return;
            }

            // Raggruppa per fila
            var rows = {};
            seatsData.forEach(function(s) {
                if (!rows[s.fila]) rows[s.fila] = [];
                rows[s.fila].push(s);
            });

            Object.keys(rows).sort().forEach(function(fila) {
                var rowDiv = document.createElement('div');
                rowDiv.className = 'seat-row';
                rowDiv.innerHTML = '<div class="row-label">' + fila + '</div>';

                rows[fila].sort(function(a, b) { return a.colonna - b.colonna; }).forEach(function(seat) {
                    var seatDiv = document.createElement('div');
                    var classes = ['seat', seat.stato];
                    if (selectedSeats.has(seat.id)) classes.push('selezione');

                    seatDiv.className = classes.join(' ');
                    seatDiv.textContent = seat.colonna;
                    seatDiv.title = 'Fila ' + seat.fila + ' - Posto ' + seat.colonna;

                    if (seat.stato === 'libero') {
                        seatDiv.addEventListener('click', function() { toggleSeat(seat.id); });
                    }

                    rowDiv.appendChild(seatDiv);
                });

                container.appendChild(rowDiv);
            });

            if (btnConfirm) btnConfirm.disabled = selectedSeats.size === 0;
        }

        function toggleSeat(seatId) {
            if (selectedSeats.has(seatId)) {
                selectedSeats.delete(seatId);
            } else {
                selectedSeats.add(seatId);
            }
            renderSeats();
        }

        if (btnCancel) {
            btnCancel.addEventListener('click', function() {
                selectedSeats.clear();
                renderSeats();
            });
        }

        if (btnConfirm) {
            btnConfirm.addEventListener('click', function() {
                if (selectedSeats.size === 0) return;

                btnConfirm.disabled = true;
                btnConfirm.textContent = 'Conferma in corso...';

                fetch('/api/book', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        evento_id: EVENT_ID,
                        posti_ids: Array.from(selectedSeats)
                    })
                })
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.success) {
                        showMessage('bookingMessage', 'Prenotazione confermata! Controlla la tua email.', 'success');
                        selectedSeats.clear();
                        loadSeats();
                    } else {
                        showMessage('bookingMessage', (data.error || 'Errore durante la prenotazione.'), 'danger');
                    }
                })
                .catch(function(e) {
                    showMessage('bookingMessage', 'Errore di rete. Riprova.', 'danger');
                })
                .finally(function() {
                    btnConfirm.disabled = false;
                    btnConfirm.textContent = 'Conferma Prenotazione';
                });
            });
        }

        // Avvia caricamento
        loadSeats();
    });
})();