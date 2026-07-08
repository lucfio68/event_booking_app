# static/js/admin.js
document.addEventListener('DOMContentLoaded', function() {
    const container = document.getElementById('adminSeatsContainer');
    if (!container) return;

    if (!window.ADMIN_CONFIG || !window.ADMIN_CONFIG.eventId) {
        container.innerHTML = '<p class="text-danger">Configurazione mancante.</p>';
        return;
    }

    const EVENT_ID = window.ADMIN_CONFIG.eventId;

    async function loadSeats() {
        try {
            const resp = await fetch('/api/seats/' + EVENT_ID);
            if (!resp.ok) {
                container.innerHTML = '<p class="text-danger">Errore caricamento posti (' + resp.status + ')</p>';
                return;
            }
            const seatsData = await resp.json();
            renderSeats(seatsData);
        } catch (e) {
            container.innerHTML = '<p class="text-danger">Errore di rete.</p>';
        }
    }

    function renderSeats(seatsData) {
        container.innerHTML = '';

        if (seatsData.length === 0) {
            container.innerHTML = '<p style="text-align:center;padding:20px;color:#7f8c8d;">Nessun posto trovato.</p>';
            return;
        }

        const maxCol = Math.max.apply(null, seatsData.map(function(s) { return s.colonna; }));

        const headerRow = document.createElement('div');
        headerRow.className = 'seat-row';
        headerRow.innerHTML = '<div class="row-label"></div>';
        for (let c = 1; c <= maxCol; c++) {
            headerRow.innerHTML += '<div class="seat col-label">' + c + '</div>';
        }
        container.appendChild(headerRow);

        const rows = {};
        seatsData.forEach(function(s) {
            if (!rows[s.fila]) rows[s.fila] = [];
            rows[s.fila].push(s);
        });

        Object.keys(rows).sort().forEach(function(fila) {
            const rowDiv = document.createElement('div');
            rowDiv.className = 'seat-row';
            rowDiv.innerHTML = '<div class="row-label">' + fila + '</div>';

            rows[fila].sort(function(a, b) { return a.colonna - b.colonna; }).forEach(function(seat) {
                const seatDiv = document.createElement('div');
                let classes = ['seat', seat.stato];
                if (seat.utente) classes.push('admin-name');

                seatDiv.className = classes.join(' ');
                seatDiv.textContent = seat.utente ? seat.utente.substring(0, 6) : seat.colonna;
                seatDiv.title = seat.utente 
                    ? 'Fila ' + seat.fila + ' Col ' + seat.colonna + ' - ' + seat.utente
                    : 'Fila ' + seat.fila + ' Col ' + seat.colonna + ' - Libero';

                if (seat.prenotazione_id) {
                    seatDiv.style.cursor = 'pointer';
                    seatDiv.addEventListener('click', function() { viewPrenotazione(seat.prenotazione_id); });
                }

                rowDiv.appendChild(seatDiv);
            });

            container.appendChild(rowDiv);
        });
    }

    loadSeats();
});

function viewPrenotazione(id) {
    fetch('/api/prenotazione/' + id)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            const modal = document.getElementById('prenotazioneModal');
            const details = document.getElementById('prenotazioneDetails');

            const postiStr = data.posti.map(function(p) { return p.fila + p.colonna; }).join(', ');

            details.innerHTML = 
                '<p><strong>ID Prenotazione:</strong> ' + data.id + '</p>' +
                '<p><strong>Utente:</strong> ' + data.utente.nome + '</p>' +
                '<p><strong>Email:</strong> ' + data.utente.email + '</p>' +
                '<p><strong>Cellulare:</strong> ' + (data.utente.cellulare || 'N/D') + '</p>' +
                '<p><strong>Data Prenotazione:</strong> ' + data.data_prenotazione + '</p>' +
                '<p><strong>Stato:</strong> ' + data.stato + '</p>' +
                '<p><strong>Posti:</strong> ' + postiStr + '</p>';

            modal.style.display = 'flex';
        })
        .catch(function(e) { alert('Errore caricamento dettagli'); });
}

function closeModal() {
    document.getElementById('prenotazioneModal').style.display = 'none';
}