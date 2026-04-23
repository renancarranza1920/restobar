(function () {
    const root = document.querySelector("[data-order-live]");
    if (!root) {
        return;
    }

    const refreshUrl = root.dataset.refreshUrl;
    const itemsPanel = root.querySelector("[data-order-items-panel]");
    const paymentPanel = root.querySelector("[data-order-payment-panel]");
    const divisionCards = root.querySelector("[data-order-division-cards]");
    const chargeStatus = root.querySelector("[data-order-charge-status]");
    const deliveredCount = root.querySelector("[data-order-delivered-count]");
    const confirmModal = document.querySelector("[data-confirm-modal]");

    let polling = null;

    function canReplace(node) {
        if (!node) {
            return false;
        }
        return !node.contains(document.activeElement);
    }

    async function refreshOrderState() {
        if (confirmModal && !confirmModal.hidden) {
            return;
        }
        if (document.body.dataset.formSubmitting === "true") {
            return;
        }

        try {
            const response = await fetch(refreshUrl, {
                headers: { Accept: "application/json" },
                cache: "no-store",
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();

            if (itemsPanel && canReplace(itemsPanel) && itemsPanel.innerHTML !== data.items_html) {
                itemsPanel.innerHTML = data.items_html;
            }

            if (paymentPanel && canReplace(paymentPanel) && paymentPanel.innerHTML !== data.payment_html) {
                paymentPanel.innerHTML = data.payment_html;
            }

            if (divisionCards && canReplace(divisionCards) && divisionCards.innerHTML !== data.division_cards_html) {
                divisionCards.innerHTML = data.division_cards_html;
            }

            if (chargeStatus && chargeStatus.innerHTML !== data.charge_status_html) {
                chargeStatus.innerHTML = data.charge_status_html;
            }

            if (deliveredCount) {
                deliveredCount.textContent = `${data.delivered_count} entregados`;
            }
        } catch (error) {
            // Keep the screen usable even if polling fails briefly.
        }
    }

    refreshOrderState();
    polling = window.setInterval(refreshOrderState, 4000);

    window.addEventListener("beforeunload", () => {
        if (polling) {
            window.clearInterval(polling);
        }
    });
})();
