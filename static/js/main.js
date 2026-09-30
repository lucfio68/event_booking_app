/* static/js/main.js
   Utility generale e SeatRenderer universale
   Aggiunta funzionalità di modifica layout per Gestione Layout Posti
*/

document.addEventListener('DOMContentLoaded', function() {
    // Chiudi modali cliccando fuori
    document.querySelectorAll('.modal').forEach(function(modal) {
        modal.addEventListener('click', function(e) {
            if (e.target === this) {
                this.style.display = 'none';
            }
        });
    });

    // Gestione pulsante "Modifica" sui layout
    document.querySelectorAll('.btn-modifica-layout').forEach(function(btn) {
        btn.addEventListener('click', function() {
            const layoutId = this.dataset.layoutId;
            if (layoutId) {
                openModifyLayoutModal(layoutId);
            }
        });
    });
});

function showMessage(elementId, message, type) {
    type = type || 'info';
    var el = document.getElementById(elementId);
    if (!el) return;
    el.className = 'alert alert-' + type;
    el.textContent = message;
    el.style.display = 'block';
    setTimeout(function() { el.style.display = 'none'; }, 5000);
}

/* === SEAT RENDERER UNIVERSALE === */
window.SeatRenderer = {
    render: function(containerId, seatsData, options) {
        options = options || {};
        var container = document.getElementById(containerId);
        if (!container) return;
        container.innerHTML = '';

        if (!seatsData || seatsData.length === 0) {
            container.innerHTML = '<p style="text-align:center;padding:20px;color:#7f8c8d;">Nessun posto trovato.</p>';
            return;
        }

        var maxCol = 0;
        for (var i = 0; i < seatsData.length; i++) {
            if (seatsData[i].colonna > maxCol) maxCol = seatsData[i].colonna;
        }

        var isAdmin = options.isAdmin || false;
        var onSeatClick = options.onSeatClick || function() {};
        var selectedIds = options.selectedIds || new Set();
        var allowDelete = options.allowDelete || false;

        // Header colonne
        var headerRow = document.createElement('div');
        headerRow.className = 'seat-row';
        var headerHtml = '<div class="row-label"></div>';
        for (var c = 1; c <= maxCol; c++) {
            headerHtml += '<div class="seat col-label">' + c + '</div>';
        }
        headerRow.innerHTML = headerHtml;
        container.appendChild(headerRow);

        // Raggruppa per fila
        var rows = {};
        for (var j = 0; j < seatsData.length; j++) {
            var s = seatsData[j];
            if (!rows[s.fila]) rows[s.fila] = [];
            rows[s.fila].push(s);
        }

        var filaKeys = Object.keys(rows).sort();
        for (var f = 0; f < filaKeys.length; f++) {
            var fila = filaKeys[f];
            var rowDiv = document.createElement('div');
            rowDiv.className = 'seat-row';
            rowDiv.innerHTML = '<div class="row-label">' + fila + '</div>';

            rows[fila].sort(function(a, b) { return a.colonna - b.colonna; });
            for (var k = 0; k < rows[fila].length; k++) {
                var seat = rows[fila][k];
                var seatDiv = document.createElement('div');
                var isSelected = selectedIds.has ? selectedIds.has(seat.id) : false;
                var classes = ['seat'];

                if (isSelected) {
                    classes.push('selezione');
                } else if (seat.stato === 'libero') {
                    classes.push('libero');
                } else if (seat.stato === 'abbonato') {
                    if (isAdmin && seat.prenotazione_id) {
                        classes.push('abbonato-admin');
                    } else {
                        classes.push('abbonato');
                    }
                } else if (seat.stato === 'riservato') {
                    classes.push('riservato');
                } else if (seat.stato === 'prenotato') {
                    if (seat.is_mio) {
                        classes.push('mio');
                    } else {
                        classes.push('prenotato');
                    }
                }

                if (seat.utente && isAdmin) classes.push('admin-name');

                seatDiv.className = classes.join(' ');
                seatDiv.textContent = (isAdmin && seat.utente) 
                    ? seat.utente.substring(0, 6) 
                    : (seat.is_mio ? 'TUO' : seat.colonna);

                seatDiv.setAttribute('data-id', seat.id);
                seatDiv.setAttribute('data-fila', seat.fila);
                seatDiv.setAttribute('data-colonna', seat.colonna);

                seatDiv.title = seat.utente 
                    ? 'Fila ' + seat.fila + ' Col ' + seat.colonna + ' - ' + seat.utente
                    : 'Fila ' + seat.fila + ' Col ' + seat.colonna + ' - ' + seat.stato;

                var isClickable = (seat.stato === 'libero') || (seat.is_mio && allowDelete) || (isAdmin && seat.prenotazione_id);
                if (isClickable) {
                    seatDiv.style.cursor = 'pointer';
                    (function(s) {
                        seatDiv.addEventListener('click', function() { onSeatClick(s); });
                    })(seat);
                }

                rowDiv.appendChild(seatDiv);
            }
            container.appendChild(rowDiv);
        }
    }
};

/* === FUNZIONE DI MODIFICA LAYOUT === */
function openModifyLayoutModal(layoutId) {
    // Carica i dati del layout e mostra il modal di modifica
    fetch(`/api/layouts/${layoutId}`)
        .then(r => r.json())
        .then(data => {
            // popola modal (id #modifyLayoutModal) con i dati
            var modal = document.getElementById('modifyLayoutModal');
            if (!modal) return;
            document.getElementById('layoutName').value = data.name;
            document.getElementById('layoutRows').value = data.rows;
            document.getElementById('layoutCols').value = data.cols;
            modal.dataset.layoutId = layoutId;
            modal.style.display = 'block';
        })
        .catch(err => showMessage('errorBox', 'Errore caricamento layout', 'danger'));
}

function submitModifyLayout() {
    var modal = document.getElementById('modifyLayoutModal');
    var layoutId = modal.dataset.layoutId;
    var payload = {
        name: document.getElementById('layoutName').value,
        rows: parseInt(document.getElementById('layoutRows').value, 10),
        cols: parseInt(document.getElementById('layoutCols').value, 10)
    };
    fetch(`/api/layouts/${layoutId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(r => {
        if (r.ok) {
            showMessage('successBox', 'Layout modificato con successo', 'success');
            modal.style.display = 'none';
            // Aggiorna la lista dei layout, se esiste
            if (typeof loadLayouts === 'function') loadLayouts();
        } else {
            return r.text().then(t => { throw new Error(t); });
        }
    })
    .catch(err => showMessage('errorBox', 'Errore modifica layout: ' + err.message, 'danger'));
}

// Event listener per il submit del form di modifica
document.addEventListener('DOMContentLoaded', function() {
    var form = document.getElementById('formModifyLayout');
    if (form) {
        form.addEventListener('submit', function(e) {
            e.preventDefault();
            submitModifyLayout();
        });
    }
});

/* === CARICA LISTA DEI LAYOUT (OPZIONALE) === */
function loadLayouts() {
    fetch('/api/layouts')
        .then(r => r.json())
        .then(data => {
            var container = document.getElementById('layoutList');
            if (!container) return;
            container.innerHTML = '';
            data.forEach(layout => {
                var div = document.createElement('div');
                div.className = 'layout-item';
                div.innerHTML = `
                    <span>${layout.name} (${layout.rows}x${layout.cols})</span>
                    <button class="btn-modifica-layout" data-layout-id="${layout.id}">Modifica</button>
                `;
                container.appendChild(div);
            });
        })
        .catch(err => showMessage('errorBox', 'Errore caricamento layout list', 'danger'));
}
