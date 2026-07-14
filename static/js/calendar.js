document.addEventListener('DOMContentLoaded', function() {
    let currentYear = new Date().getFullYear();
    let currentMonth = new Date().getMonth(); // 0-11
    let selectedDate = null;
    let eventsData = {};

    const yearEl = document.getElementById('currentYear');
    const monthEl = document.getElementById('currentMonth');
    const calendarGrid = document.getElementById('calendarGrid');
    const actionsDiv = document.getElementById('calendarActions');
    const selectedDateSpan = document.getElementById('selectedDate');
    const btnCreateEvent = document.getElementById('btnCreateEvent');
    const btnBook = document.getElementById('btnBook');

    const monthNames = ['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno',
                        'Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre'];

    function init() {
        document.getElementById('prevYear').addEventListener('click', () => { currentYear--; updateCalendar(); });
        document.getElementById('nextYear').addEventListener('click', () => { currentYear++; updateCalendar(); });
        document.getElementById('prevMonth').addEventListener('click', () => {
            currentMonth--;
            if (currentMonth < 0) { currentMonth = 11; currentYear--; }
            updateCalendar();
        });
        document.getElementById('nextMonth').addEventListener('click', () => {
            currentMonth++;
            if (currentMonth > 11) { currentMonth = 0; currentYear++; }
            updateCalendar();
        });

        if (btnCreateEvent) {
            btnCreateEvent.addEventListener('click', () => {
                if (selectedDate) {
                    window.location.href = `/event/create?date=${selectedDate}`;
                }
            });
        }

        if (btnBook) {
            btnBook.addEventListener('click', () => {
                if (selectedDate && eventsData[selectedDate]) {
                    const day = parseInt(selectedDate.split('-')[2]);
                    if (eventsData[day] && eventsData[day].length > 0) {
                        window.location.href = `/booking/${eventsData[day][0].id}`;
                    }
                }
            });
        }

        document.querySelector('.close-modal').addEventListener('click', function() {
            document.getElementById('dayEventsModal').style.display = 'none';
        });

        updateCalendar();
    }

    async function updateCalendar() {
        yearEl.textContent = currentYear;
        monthEl.textContent = monthNames[currentMonth];

        try {
            const resp = await fetch(`/api/events?year=${currentYear}&month=${currentMonth + 1}`);
            eventsData = await resp.json();
        } catch (e) {
            console.error('Errore caricamento eventi:', e);
            eventsData = {};
        }

        renderDays();
    }

    function renderDays() {
        calendarGrid.innerHTML = '';

        // Header giorni della settimana
        const headers = ['Lun','Mar','Mer','Gio','Ven','Sab','Dom'];
        headers.forEach(h => {
            const div = document.createElement('div');
            div.className = 'day-header';
            div.textContent = h;
            calendarGrid.appendChild(div);
        });

        const firstDay = new Date(currentYear, currentMonth, 1);
        const lastDay = new Date(currentYear, currentMonth + 1, 0);
        const daysInMonth = lastDay.getDate();

        // Giorno della settimana del primo giorno (0=Dom, 1=Lun...)
        let startDay = firstDay.getDay();
        if (startDay === 0) startDay = 7; // Domenica -> 7
        startDay--; // 0=Lun, 6=Dom

        const today = new Date();
        const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;

        // Celle vuote prima
        for (let i = 0; i < startDay; i++) {
            const cell = document.createElement('div');
            cell.className = 'day-cell empty';
            calendarGrid.appendChild(cell);
        }

        // Giorni
        for (let day = 1; day <= daysInMonth; day++) {
            const cell = document.createElement('div');
            const dateStr = `${currentYear}-${String(currentMonth+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
            const isPast = dateStr < todayStr;
            const isToday = dateStr === todayStr;
            const hasEvents = eventsData[day] && eventsData[day].length > 0;

            let classes = ['day-cell'];
            if (isPast) classes.push('past');
            if (isToday) classes.push('today');
            if (hasEvents) classes.push('has-event');
            if (selectedDate === dateStr) classes.push('selected');

            cell.className = classes.join(' ');
            cell.dataset.date = dateStr;

            let html = `<div class="day-number">${day}</div>`;
            if (hasEvents) {
                eventsData[day].forEach(ev => {
                    html += `<div class="day-event" title="${ev.nome} - ${ev.ora}">${ev.nome}</div>`;
                });
            }
            cell.innerHTML = html;

            // Click singolo: selezione
            cell.addEventListener('click', function() {
                if (isPast) return;
                selectedDate = dateStr;
                selectedDateSpan.textContent = dateStr;
                actionsDiv.style.display = 'block';

                if (btnBook) {
                    btnBook.disabled = !hasEvents;
                }

                renderDays(); // aggiorna selezione visiva
            });

            // Doppio click: visualizza eventi/prenotazioni
            cell.addEventListener('dblclick', function() {
                if (hasEvents) {
                    showDayEvents(day, dateStr);
                }
            });

            calendarGrid.appendChild(cell);
        }
    }

    async function showDayEvents(day, dateStr) {
        const modal = document.getElementById('dayEventsModal');
        const list = document.getElementById('dayEventsList');
        const preview = document.getElementById('seatsPreview');
        document.getElementById('modalDate').textContent = dateStr;

        let html = '<div class="event-list">';
        for (const ev of eventsData[day]) {
            html += `
                <div class="event-card" style="padding:15px; border:1px solid #ddd; margin:10px 0; border-radius:8px;">
                    <h4>${ev.nome}</h4>
                    <p>Ora: ${ev.ora} | Sala: ${ev.sala}</p>
                    <a href="/booking/${ev.id}" class="btn btn-success btn-sm">Prenota</a>
                    ${isAdmin ? `<a href="/admin/event/${ev.id}" class="btn btn-info btn-sm">Gestisci</a><button class="btn btn-danger btn-sm btn-delete-event" data-event-id="${ev.id}" data-event-name="${ev.nome.replace(/"/g, '&quot;')}" style="margin-left:5px;">Elimina</button>` : ''}
                </div>
            `;
        }
        html += '</div>';
        list.innerHTML = html;

        preview.innerHTML = '<h4>Posti Prenotati</h4><p>Seleziona un evento per i dettagli.</p>';

        modal.style.display = 'flex';
    }

    init();

    // Setup delete event buttons in modal
    document.getElementById("dayEventsList").addEventListener("click", function(e) {
        var btn = e.target.closest(".btn-delete-event");
        if (!btn) return;
        var eventId = btn.dataset.eventId;
        var eventName = btn.dataset.eventName;
        deleteEventJs(parseInt(eventId), eventName);
    });
});

function deleteEventJs(eventId, eventName) {
    if (!confirm("Eliminare evento: " + eventName + "?\n\nVerranno eliminate tutte le prenotazioni.\nOperazione irreversibile.")) {
        return;
    }
    fetch("/api/event/delete/" + eventId, {method: "POST"})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                alert("Evento eliminato!");
                document.getElementById("dayEventsModal").style.display = "none";
                location.reload();
            } else {
                alert("Errore: " + (data.error || "Eliminazione fallita"));
            }
        })
        .catch(function(e) {
            alert("Errore di rete");
        });
}
