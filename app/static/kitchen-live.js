(function () {
    const root = document.querySelector("[data-kitchen-live]");
    if (!root) {
        return;
    }

    const apiUrl = root.dataset.apiUrl;
    const actionTemplate = root.dataset.actionTemplate || "";
    const queue = document.querySelector("[data-kitchen-queue]");
    const countNode = document.querySelector("[data-kitchen-count]");
    const statusNode = document.querySelector("[data-kitchen-status]");
    const lastUpdateNode = document.querySelector("[data-last-update]");
    const bannerNode = document.querySelector("[data-kitchen-banner]");
    const alertButton = document.querySelector("[data-alert-toggle]");
    const alertStatusNode = document.querySelector("[data-alert-status]");
    const alertWordNode = document.querySelector("[data-kitchen-alert-word]");
    const storageKey = "restobar:kitchen-alerts-enabled";

    let knownIds = new Set(
        (root.dataset.initialIds || "")
            .split(",")
            .map((value) => parseInt(value, 10))
            .filter((value) => !Number.isNaN(value))
    );
    let alertsEnabled = window.localStorage.getItem(storageKey) === "true";
    let notificationEnabled = "Notification" in window && Notification.permission === "granted";
    let audioUnlocked = false;
    let polling = null;

    function escapeHtml(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function persistAlertState() {
        window.localStorage.setItem(storageKey, alertsEnabled ? "true" : "false");
    }

    function updateAlertCopy() {
        const label = alertsEnabled ? "Alertas activadas" : "Alertas apagadas";
        if (alertStatusNode) {
            alertStatusNode.textContent = label;
        }
        if (alertWordNode) {
            alertWordNode.textContent = alertsEnabled ? "On" : "Off";
        }
        if (alertButton) {
            alertButton.innerHTML = alertsEnabled
                ? '<i class="fa-solid fa-bell mr-2"></i> Alertas activas'
                : '<i class="fa-solid fa-bell mr-2"></i> Activar alertas';
        }
    }

    function playTone() {
        if (!alertsEnabled || !audioUnlocked) {
            return;
        }

        const AudioContextRef = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextRef) {
            return;
        }

        const context = new AudioContextRef();
        const oscillator = context.createOscillator();
        const gain = context.createGain();

        oscillator.type = "triangle";
        oscillator.frequency.setValueAtTime(880, context.currentTime);
        gain.gain.setValueAtTime(0.0001, context.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.18, context.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.42);

        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start();
        oscillator.stop(context.currentTime + 0.45);
        oscillator.onended = () => context.close();
    }

    function armAudioUnlock() {
        if (!alertsEnabled || audioUnlocked) {
            return;
        }

        const unlockAudio = () => {
            audioUnlocked = true;
            window.removeEventListener("pointerdown", unlockAudio);
            window.removeEventListener("keydown", unlockAudio);
        };

        window.addEventListener("pointerdown", unlockAudio, { once: true, passive: true });
        window.addEventListener("keydown", unlockAudio, { once: true });
    }

    async function enableAlerts() {
        alertsEnabled = true;
        audioUnlocked = true;
        persistAlertState();
        updateAlertCopy();
        playTone();

        if ("Notification" in window && Notification.permission === "default") {
            const permission = await Notification.requestPermission();
            notificationEnabled = permission === "granted";
        } else if ("Notification" in window) {
            notificationEnabled = Notification.permission === "granted";
        }
    }

    function showBanner(message) {
        if (!bannerNode) {
            return;
        }
        bannerNode.hidden = false;
        bannerNode.textContent = message;
        bannerNode.classList.add("is-visible");

        window.clearTimeout(showBanner.timer);
        showBanner.timer = window.setTimeout(() => {
            bannerNode.classList.remove("is-visible");
            bannerNode.hidden = true;
        }, 4500);
    }

    function maybeNotify(newItems) {
        if (!alertsEnabled || newItems.length === 0) {
            return;
        }

        showBanner(`Nueva alerta de cocina: ${newItems.length} item(s) entraron a la cola.`);
        playTone();

        if (notificationEnabled && document.visibilityState !== "visible") {
            const first = newItems[0];
            new Notification("Nueva orden en cocina", {
                body: `${first.product} para ${first.table}`,
            });
        }
    }

    function renderCard(item) {
        return `
            <article class="kitchen-ticket" data-item-id="${item.id}">
                <div class="kitchen-ticket-head">
                    <div class="kitchen-ticket-ident">
                        <span class="kitchen-ticket-qty">x${item.quantity}</span>
                        <div class="kitchen-ticket-copy">
                            <strong class="kitchen-ticket-title">${escapeHtml(item.product)}</strong>
                            <div class="kitchen-ticket-tags">
                                <span class="kitchen-ticket-tag">Orden #${item.order_id}</span>
                                <span class="kitchen-ticket-tag">${escapeHtml(item.table)}</span>
                                <span class="kitchen-ticket-tag">${escapeHtml(item.category)}</span>
                            </div>
                        </div>
                    </div>
                    <div class="kitchen-ticket-status">
                        <span class="status-pill status-pendiente">Pendiente</span>
                        <span class="kitchen-ticket-wait">${escapeHtml(item.wait_label)}</span>
                    </div>
                </div>
                <div class="kitchen-ticket-grid">
                    <div class="kitchen-ticket-block">
                        <span class="kitchen-ticket-label">Mesa</span>
                        <strong>${escapeHtml(item.table)}</strong>
                    </div>
                    ${item.customer ? `
                        <div class="kitchen-ticket-block">
                            <span class="kitchen-ticket-label">Cliente</span>
                            <strong>${escapeHtml(item.customer)}</strong>
                        </div>
                    ` : ""}
                </div>
                ${item.notes ? `
                    <div class="kitchen-ticket-notes">
                        <span class="kitchen-ticket-label">Notas</span>
                        <p>${escapeHtml(item.notes)}</p>
                    </div>
                ` : ""}
                <form method="post" action="${escapeHtml(actionTemplate.replace("__ID__", String(item.id)))}" data-confirm-title="Marcar item listo" data-confirm-message="El producto ${escapeHtml(item.product)} quedara listo y entregado en la orden.">
                    <button class="button button-primary button-full kitchen-ticket-action" type="submit">
                        <i class="fa-solid fa-check mr-2"></i> Marcar listo
                    </button>
                </form>
            </article>
        `;
    }

    function renderItems(items) {
        if (!queue) {
            return;
        }

        if (!items.length) {
            queue.innerHTML = `
                <div class="empty-state-block" data-kitchen-empty>
                    <p class="empty-state">No hay platillos pendientes en este momento.</p>
                </div>
            `;
            return;
        }

        queue.innerHTML = items.map(renderCard).join("");
    }

    function refreshMeta(count, updatedAt) {
        if (countNode) {
            countNode.textContent = String(count);
        }
        if (statusNode) {
            statusNode.textContent = `${count} en cola`;
        }
        if (lastUpdateNode) {
            const dateValue = new Date();
            lastUpdateNode.textContent = `Actualizado ${dateValue.toLocaleTimeString(navigator.language || "es-SV")}`;
        }
    }

    async function fetchQueue() {
        try {
            const response = await fetch(apiUrl, {
                headers: { Accept: "application/json" },
                cache: "no-store",
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();
            const incomingIds = new Set(data.items.map((item) => item.id));
            const newItems = data.items.filter((item) => !knownIds.has(item.id));

            renderItems(data.items);
            refreshMeta(data.count, data.updated_at);
            maybeNotify(newItems);
            knownIds = incomingIds;
        } catch (error) {
            if (statusNode) {
                statusNode.textContent = "Sin conexion con cocina";
            }
        }
    }

    if (alertButton) {
        alertButton.addEventListener("click", enableAlerts);
    }

    updateAlertCopy();
    armAudioUnlock();
    fetchQueue();
    polling = window.setInterval(fetchQueue, 5000);

    window.addEventListener("beforeunload", () => {
        if (polling) {
            window.clearInterval(polling);
        }
    });
})();
