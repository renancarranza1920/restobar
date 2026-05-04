(function () {
    const scrollKey = "restobar:scroll-restore";
    const confirmModal = document.querySelector("[data-confirm-modal]");
    const confirmTitle = confirmModal?.querySelector(".confirm-dialog-title[data-confirm-title]");
    const confirmMessage = confirmModal?.querySelector(".confirm-dialog-message[data-confirm-message]");
    const confirmAccept = confirmModal?.querySelector("[data-confirm-accept]");
    const confirmQuantityWrap = confirmModal?.querySelector("[data-confirm-quantity-wrap]");
    const confirmQuantityInput = confirmModal?.querySelector("[data-confirm-quantity]");
    const confirmCancelButtons = confirmModal
        ? confirmModal.querySelectorAll("[data-confirm-cancel]")
        : [];
    const paymentModal = document.querySelector("[data-payment-modal]");
    const paymentTitle = paymentModal?.querySelector("[data-payment-title]");
    const paymentAmountNode = paymentModal?.querySelector("[data-payment-amount]");
    const paymentReceivedWrap = paymentModal?.querySelector("[data-payment-received-wrap]");
    const paymentReceivedInput = paymentModal?.querySelector("[data-payment-received]");
    const paymentChangeBox = paymentModal?.querySelector("[data-payment-change-box]");
    const paymentChangeLabel = paymentModal?.querySelector("[data-payment-change-label]");
    const paymentChangeNode = paymentModal?.querySelector("[data-payment-change]");
    const paymentMessage = paymentModal?.querySelector("[data-payment-message]");
    const paymentAccept = paymentModal?.querySelector("[data-payment-accept]");
    const paymentCancelButtons = paymentModal
        ? paymentModal.querySelectorAll("[data-payment-cancel]")
        : [];
    const sidebar = document.querySelector("[data-sidebar]");
    const sidebarBackdrop = document.querySelector("[data-sidebar-backdrop]");
    const sidebarCollapseToggle = document.querySelector("[data-sidebar-collapse-toggle]");
    const sidebarCollapsedKey = "restobar:sidebar-collapsed";

    let pendingConfirmAction = null;
    let pendingPaymentForm = null;
    let pendingPaymentAmount = 0;
    let pendingPaymentIsCash = true;

    if ("scrollRestoration" in window.history) {
        window.history.scrollRestoration = "manual";
    }

    function setFormSubmittingState(isSubmitting) {
        if (isSubmitting) {
            document.body.dataset.formSubmitting = "true";
            return;
        }
        delete document.body.dataset.formSubmitting;
    }

    function currentPathKey() {
        return `${window.location.pathname}${window.location.search}`;
    }

    function currentPathname() {
        return window.location.pathname;
    }

    function storeScrollPosition(path = currentPathKey(), pathname = currentPathname()) {
        sessionStorage.setItem(
            scrollKey,
            JSON.stringify({
                path,
                pathname,
                y: window.scrollY || window.pageYOffset || 0,
            })
        );
    }

    function restoreScrollPosition() {
        const raw = sessionStorage.getItem(scrollKey);
        if (!raw) {
            return;
        }

        try {
            const data = JSON.parse(raw);
            if (data.path !== currentPathKey() && data.pathname !== currentPathname()) {
                return;
            }

            const y = Number(data.y || 0);
            [0, 120, 320, 700].forEach((delay) => {
                window.setTimeout(() => window.scrollTo(0, y), delay);
            });
        } catch (error) {
            // Ignore malformed storage values.
        } finally {
            sessionStorage.removeItem(scrollKey);
        }
    }

    function ensureFlashContainer() {
        let container = document.querySelector(".flash-container");
        if (container) {
            return container;
        }

        const contentWrapper = document.querySelector(".content-wrapper");
        if (!contentWrapper) {
            return null;
        }

        container = document.createElement("div");
        container.className = "flash-container";
        contentWrapper.parentNode.insertBefore(container, contentWrapper);
        return container;
    }

    function dismissFlash(node) {
        if (!node) {
            return;
        }
        node.classList.add("is-hiding");
        window.setTimeout(() => node.remove(), 220);
    }

    function scheduleFlashDismiss(node) {
        const timeout = node.classList.contains("flash-error") ? 7000 : 4500;
        window.setTimeout(() => dismissFlash(node), timeout);
    }

    function showLocalFlash(message, category) {
        const container = ensureFlashContainer();
        if (!container) {
            window.alert(message);
            return;
        }

        const icons = {
            success: "fa-circle-check",
            info: "fa-circle-info",
            warning: "fa-triangle-exclamation",
            error: "fa-circle-exclamation",
        };

        const flash = document.createElement("div");
        flash.className = `flash-message flash-${category || "info"} shadow-sm`;
        flash.setAttribute("data-flash", "");
        flash.innerHTML = `
            <div class="flash-icon">
                <i class="fa-solid ${icons[category] || icons.info}"></i>
            </div>
            <div class="flash-copy"></div>
            <button class="flash-close" type="button" data-flash-close aria-label="Cerrar alerta">
                <i class="fa-solid fa-xmark"></i>
            </button>
        `;

        const copy = flash.querySelector(".flash-copy");
        if (copy) {
            copy.textContent = message;
        }

        container.appendChild(flash);
        scheduleFlashDismiss(flash);
    }

    function localDateLocale() {
        return navigator.language || "es-SV";
    }

    function configuredTimeZone() {
        return document.body?.dataset?.appTimezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    }

    function configuredDateFormat() {
        return document.body?.dataset?.dateFormat || "dd/mm/yyyy";
    }

    function configuredTimeFormat() {
        return document.body?.dataset?.timeFormat || "12h";
    }

    function parseUtcDate(rawValue) {
        if (!rawValue) {
            return null;
        }

        const normalizedValue =
            /[zZ]$|[+\-]\d{2}:\d{2}$/.test(rawValue) ? rawValue : `${rawValue}Z`;
        const parsedDate = new Date(normalizedValue);
        return Number.isNaN(parsedDate.getTime()) ? null : parsedDate;
    }

    function datePartsForTimeZone(dateValue) {
        const formatter = new Intl.DateTimeFormat("en-US", {
            timeZone: configuredTimeZone(),
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
        });
        const parts = {};
        formatter.formatToParts(dateValue).forEach((part) => {
            if (part.type !== "literal") {
                parts[part.type] = part.value;
            }
        });
        return parts;
    }

    function formatDatePart(dateValue) {
        const parts = datePartsForTimeZone(dateValue);
        const format = configuredDateFormat();
        if (format === "yyyy-mm-dd") {
            return `${parts.year}-${parts.month}-${parts.day}`;
        }
        if (format === "mm/dd/yyyy") {
            return `${parts.month}/${parts.day}/${parts.year}`;
        }
        return `${parts.day}/${parts.month}/${parts.year}`;
    }

    function formatTimePart(dateValue) {
        return dateValue.toLocaleTimeString(localDateLocale(), {
            timeZone: configuredTimeZone(),
            hour: "numeric",
            minute: "2-digit",
            hour12: configuredTimeFormat() !== "24h",
        });
    }

    function formatDateObject(dateValue, mode) {
        if (!dateValue) {
            return "";
        }
        if (mode === "date") {
            return formatDatePart(dateValue);
        }
        if (mode === "time") {
            return formatTimePart(dateValue);
        }
        return `${formatDatePart(dateValue)} ${formatTimePart(dateValue)}`;
    }

    function formatLocalDate(rawValue, mode) {
        return formatDateObject(parseUtcDate(rawValue), mode);
    }

    function renderLocalDateNodes() {
        document.querySelectorAll("[data-local-datetime]").forEach((node) => {
            const value = formatLocalDate(
                node.getAttribute("data-local-datetime"),
                node.getAttribute("data-local-format") || "datetime"
            );
            if (value) {
                node.textContent = value;
            }
        });

        document.querySelectorAll("[data-local-current-date]").forEach((node) => {
            node.textContent = formatDateObject(new Date(), "date");
        });

        document.querySelectorAll("[data-local-current-datetime]").forEach((node) => {
            node.textContent = formatDateObject(
                new Date(),
                node.getAttribute("data-local-format") || "datetime"
            );
        });
    }

    window.RestobarDates = {
        formatLocalDate,
        renderLocalDateNodes,
    };

    function closeConfirmModal() {
        if (!confirmModal) {
            pendingConfirmAction = null;
            return;
        }

        pendingConfirmAction = null;
        if (confirmQuantityWrap) {
            confirmQuantityWrap.hidden = true;
        }
        if (confirmQuantityInput) {
            confirmQuantityInput.value = "";
            confirmQuantityInput.removeAttribute("max");
        }
        confirmModal.hidden = true;
        document.body.classList.remove("modal-open");
    }

    function openConfirmModal(title, message, onAccept, options = {}) {
        if (!confirmModal || !confirmTitle || !confirmMessage || !confirmAccept) {
            const fallbackMessage = message || title || "¿Deseas continuar?";
            if (window.confirm(fallbackMessage)) {
                onAccept();
            }
            return;
        }

        pendingConfirmAction = onAccept;
        confirmTitle.textContent = title || "¿Deseas continuar?";
        confirmMessage.textContent = message || "Confirma esta acción para continuar.";
        if (confirmQuantityWrap && confirmQuantityInput) {
            confirmQuantityWrap.hidden = !options.quantity;
            if (options.quantity) {
                confirmQuantityInput.value = String(options.quantity.value || 1);
                confirmQuantityInput.max = String(options.quantity.max || 1);
                confirmQuantityInput.min = "1";
            }
        }
        confirmModal.hidden = false;
        document.body.classList.add("modal-open");
        if (options.quantity && confirmQuantityInput) {
            confirmQuantityInput.focus();
            confirmQuantityInput.select();
        } else {
            confirmAccept.focus();
        }
    }

    function moneyFormatter() {
        return new Intl.NumberFormat(navigator.language || "es-SV", {
            style: "currency",
            currency: "USD",
        });
    }

    function formatMoney(value) {
        return moneyFormatter().format(Number(value || 0));
    }

    function parsePaymentValue(value) {
        const normalized = String(value || "0").replace(",", ".");
        const parsed = Number.parseFloat(normalized);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function paymentAmountForForm(form) {
        const explicitAmount = form.getAttribute("data-payment-amount");
        if (explicitAmount) {
            return parsePaymentValue(explicitAmount);
        }

        const amountField = form.querySelector("[name='amount']");
        return parsePaymentValue(amountField?.value);
    }

    function paymentMethodForForm(form) {
        return (form.querySelector("[name='method']")?.value || "efectivo").toLowerCase();
    }

    function setHiddenFormValue(form, name, value) {
        let input = form.querySelector(`input[name="${name}"]`);
        if (!input) {
            input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            form.appendChild(input);
        }
        input.value = value;
    }

    function writePaymentTenderFields(form) {
        const received = pendingPaymentIsCash
            ? parsePaymentValue(paymentReceivedInput?.value)
            : pendingPaymentAmount;
        const change = pendingPaymentIsCash ? Math.max(received - pendingPaymentAmount, 0) : 0;
        setHiddenFormValue(form, "tendered_amount", received.toFixed(2));
        setHiddenFormValue(form, "change_amount", change.toFixed(2));
    }

    function submitConfirmedPaymentForm(form) {
        if (!form) {
            return;
        }

        form.dataset.paymentConfirmed = "true";
        if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
            return;
        }

        setFormSubmittingState(true);
        if ((form.method || "get").toLowerCase() === "post") {
            storeScrollPosition();
        }
        HTMLFormElement.prototype.submit.call(form);
    }

    function updatePaymentChange() {
        if (!paymentReceivedInput || !paymentChangeNode || !paymentMessage || !paymentAccept) {
            return;
        }

        if (!pendingPaymentIsCash) {
            paymentChangeNode.textContent = formatMoney(0);
            paymentMessage.textContent = "Confirma que el cobro con tarjeta fue aprobado.";
            paymentAccept.disabled = false;
            paymentChangeBox?.classList.remove("is-warning");
            return;
        }

        const received = parsePaymentValue(paymentReceivedInput.value);
        const change = received - pendingPaymentAmount;
        const hasEnoughCash = received >= pendingPaymentAmount;
        paymentChangeNode.textContent = formatMoney(Math.max(change, 0));
        paymentMessage.textContent = hasEnoughCash
            ? "Entrega el cambio indicado antes de cerrar el pago."
            : `Faltan ${formatMoney(Math.abs(change))} para completar el cobro.`;
        paymentAccept.disabled = !hasEnoughCash;
        paymentChangeBox?.classList.toggle("is-warning", !hasEnoughCash);
    }

    function closePaymentModal() {
        pendingPaymentForm = null;
        pendingPaymentAmount = 0;
        pendingPaymentIsCash = true;
        if (!paymentModal) {
            return;
        }

        paymentModal.hidden = true;
        document.body.classList.remove("modal-open");
    }

    function openPaymentModal(form) {
        pendingPaymentForm = form;
        pendingPaymentAmount = paymentAmountForForm(form);
        pendingPaymentIsCash = paymentMethodForForm(form) === "efectivo";

        if (!paymentModal || !paymentTitle || !paymentAmountNode || !paymentAccept) {
            submitForm(form);
            return;
        }

        const label = form.getAttribute("data-payment-label") || "Pago";
        paymentTitle.textContent = pendingPaymentIsCash ? `Pago en efectivo - ${label}` : `Pago con tarjeta - ${label}`;
        paymentAmountNode.textContent = formatMoney(pendingPaymentAmount);

        if (paymentReceivedWrap && paymentReceivedInput) {
            paymentReceivedWrap.hidden = !pendingPaymentIsCash;
            paymentReceivedInput.value = pendingPaymentIsCash ? pendingPaymentAmount.toFixed(2) : "";
        }
        if (paymentChangeLabel) {
            paymentChangeLabel.textContent = pendingPaymentIsCash ? "Cambio a entregar" : "Cambio";
        }

        paymentModal.hidden = false;
        document.body.classList.add("modal-open");
        updatePaymentChange();

        if (pendingPaymentIsCash && paymentReceivedInput) {
            paymentReceivedInput.focus();
            paymentReceivedInput.select();
        } else {
            paymentAccept.focus();
        }
    }

    function openCancelItemConfirm(form, title, message) {
        const maxQuantity = Math.max(Number.parseInt(form.dataset.cancelItemMax || "1", 10) || 1, 1);
        const quantityInput = form.querySelector("[data-cancel-item-quantity]");
        openConfirmModal(
            title,
            message,
            () => {
                const quantity = Math.max(
                    1,
                    Math.min(
                        Number.parseInt(confirmQuantityInput?.value || "1", 10) || 1,
                        maxQuantity
                    )
                );
                if (quantityInput) {
                    quantityInput.value = String(quantity);
                }
                submitForm(form);
            },
            { quantity: { value: maxQuantity, max: maxQuantity } }
        );
    }

    function submitForm(form) {
        if (!form) {
            return;
        }

        setFormSubmittingState(true);
        if ((form.method || "get").toLowerCase() === "post") {
            storeScrollPosition();
        }

        HTMLFormElement.prototype.submit.call(form);
    }

    function validateSplitForm(form) {
        const rows = form.querySelectorAll("[data-split-row]");
        for (const row of rows) {
            const total = Number(row.dataset.itemQuantity || 0);
            const itemName = row.dataset.itemName || "el producto";
            const inputs = row.querySelectorAll("[data-split-input]");
            let assigned = 0;

            for (const input of inputs) {
                const value = Number(input.value || 0);
                if (value < 0) {
                    return `No puedes usar cantidades negativas en ${itemName}.`;
                }
                if (value > total) {
                    return `No puedes asignar más de ${total} unidades en ${itemName}.`;
                }
                assigned += value;
            }

            if (assigned !== total) {
                return `Debes repartir exactamente ${total} unidades de ${itemName}.`;
            }
        }

        return null;
    }

    function wireSplitWorkspace() {
        const workspace = document.querySelector("[data-split-workspace]");
        if (!workspace || workspace.dataset.splitWired === "true") {
            return;
        }

        workspace.dataset.splitWired = "true";

        const moneyFormatter = new Intl.NumberFormat(navigator.language || "es-SV", {
            style: "currency",
            currency: "USD",
        });
        const cards = workspace.querySelectorAll("[data-split-person-card]");
        const labels = workspace.querySelectorAll("[data-person-label]");
        const activePersonLabel = workspace.querySelector("[data-active-person-label]");
        const remainingTotalNode = workspace.querySelector("[data-split-remaining-total]");
        const statusNode = workspace.querySelector("[data-split-status]");
        const submitButton = workspace.querySelector("[data-split-submit]");
        let activePerson = "1";

        function cleanQuantity(input, maxValue) {
            const nextValue = Number.parseInt(input?.value || "0", 10);
            if (Number.isNaN(nextValue) || nextValue < 0) {
                return 0;
            }
            return Math.min(nextValue, maxValue);
        }

        function personName(person) {
            const input = workspace.querySelector(`[data-person-label="${person}"]`);
            const rawName = (input?.value || "").trim();
            return rawName || `Cliente ${person}`;
        }

        function setActivePerson(person) {
            activePerson = String(person || "1");
            cards.forEach((card) => {
                card.classList.toggle("is-active", card.dataset.person === activePerson);
            });
            if (activePersonLabel) {
                activePersonLabel.textContent = personName(activePerson);
            }
        }

        function refreshLabels() {
            labels.forEach((input) => {
                const person = input.dataset.personLabel;
                const name = personName(person);
                workspace.querySelectorAll(`[data-cell-label="${person}"]`).forEach((node) => {
                    node.textContent = name;
                });
                workspace.querySelectorAll(`[data-person-name-display="${person}"]`).forEach((node) => {
                    node.textContent = name;
                });
            });
            if (activePersonLabel) {
                activePersonLabel.textContent = personName(activePerson);
            }
        }

        function rowInput(row, person) {
            return row?.querySelector(`[data-split-input][data-person="${person}"]`);
        }

        function assignedInRow(row, maxValue) {
            return Array.from(row.querySelectorAll("[data-split-input]")).reduce(
                (total, input) => total + cleanQuantity(input, maxValue),
                0
            );
        }

        function updateWorkspace() {
            const totals = {};
            const drinks = {};
            const food = {};
            let remainingMoney = 0;
            let remainingUnits = 0;
            let hasOverflow = false;

            workspace.querySelectorAll("[data-split-row]").forEach((row) => {
                const totalUnits = Number.parseInt(row.dataset.itemQuantity || "0", 10) || 0;
                const price = Number.parseFloat(row.dataset.itemPrice || "0") || 0;
                const kind = row.dataset.itemKind || "drink";
                let assigned = 0;

                row.querySelectorAll("[data-split-input]").forEach((input) => {
                    const person = input.dataset.person;
                    const qty = cleanQuantity(input, totalUnits);
                    input.value = qty;
                    assigned += qty;
                    totals[person] = (totals[person] || 0) + qty * price;
                    if (kind === "food") {
                        food[person] = (food[person] || 0) + qty;
                    } else {
                        drinks[person] = (drinks[person] || 0) + qty;
                    }
                });

                const remaining = Math.max(totalUnits - assigned, 0);
                const remainingNode = row.querySelector("[data-row-remaining]");
                const activeQtyNode = row.querySelector("[data-active-row-qty]");
                const activeInput = rowInput(row, activePerson);
                if (remainingNode) {
                    remainingNode.textContent = remaining;
                }
                if (activeQtyNode) {
                    activeQtyNode.textContent = cleanQuantity(activeInput, totalUnits);
                }
                row.querySelectorAll("[data-person-chip]").forEach((chip) => {
                    const person = chip.dataset.personChip;
                    const input = rowInput(row, person);
                    const qty = cleanQuantity(input, totalUnits);
                    chip.classList.toggle("is-active", person === activePerson);
                    chip.classList.toggle("has-qty", qty > 0);
                    const qtyNode = chip.querySelector(`[data-person-chip-qty="${person}"]`);
                    if (qtyNode) {
                        qtyNode.textContent = qty;
                    }
                });
                row.classList.toggle("is-complete", remaining === 0 && assigned === totalUnits);
                row.classList.toggle("has-overflow", assigned > totalUnits);
                hasOverflow = hasOverflow || assigned > totalUnits;
                remainingUnits += remaining;
                remainingMoney += remaining * price;
            });

            cards.forEach((card) => {
                const person = card.dataset.person;
                const totalNode = workspace.querySelector(`[data-person-total="${person}"]`);
                const drinksNode = workspace.querySelector(`[data-person-drinks="${person}"]`);
                const foodNode = workspace.querySelector(`[data-person-food="${person}"]`);
                if (totalNode) {
                    totalNode.textContent = moneyFormatter.format(totals[person] || 0);
                }
                if (drinksNode) {
                    drinksNode.textContent = drinks[person] || 0;
                }
                if (foodNode) {
                    foodNode.textContent = food[person] || 0;
                }
            });

            if (remainingTotalNode) {
                remainingTotalNode.textContent = moneyFormatter.format(remainingMoney);
            }
            if (statusNode) {
                statusNode.textContent = hasOverflow
                    ? "Hay productos de mas"
                    : remainingUnits === 0
                        ? "Listo para guardar"
                        : `${remainingUnits} unidad${remainingUnits === 1 ? "" : "es"} pendiente${remainingUnits === 1 ? "" : "s"}`;
                statusNode.classList.toggle("text-success", remainingUnits === 0 && !hasOverflow);
            }
            if (submitButton && !submitButton.disabled) {
                submitButton.classList.toggle("button-success", remainingUnits === 0);
                submitButton.classList.toggle("button-secondary", remainingUnits !== 0);
            }

            refreshLabels();
        }

        workspace.addEventListener("click", (event) => {
            const selector = event.target.closest("[data-split-person-select]");
            if (selector) {
                setActivePerson(selector.dataset.splitPersonSelect);
                updateWorkspace();
                return;
            }

            const stepButton = event.target.closest("[data-split-active-step]");
            if (!stepButton) {
                return;
            }

            const row = event.target.closest("[data-split-row]");
            const input = rowInput(row, activePerson);
            if (!row || !input) {
                return;
            }

            const maxValue = Number.parseInt(row.dataset.itemQuantity || "0", 10) || 0;
            const step = Number.parseInt(stepButton.dataset.splitActiveStep || "0", 10) || 0;
            if (step > 0 && assignedInRow(row, maxValue) >= maxValue) {
                updateWorkspace();
                return;
            }
            input.value = cleanQuantity(input, maxValue) + step;
            input.value = cleanQuantity(input, maxValue);
            updateWorkspace();
        });

        workspace.addEventListener("input", (event) => {
            if (event.target.matches("[data-split-input]")) {
                updateWorkspace();
            }
            if (event.target.matches("[data-person-label]")) {
                refreshLabels();
            }
        });

        setActivePerson(activePerson);
        updateWorkspace();
    }

    function wireFlashMessages() {
        document.querySelectorAll("[data-flash]").forEach((flash) => {
            if (flash.dataset.dismissWired === "true") {
                return;
            }
            flash.dataset.dismissWired = "true";
            scheduleFlashDismiss(flash);
        });
    }

    function openSidebar() {
        if (!sidebar) {
            return;
        }
        document.body.classList.add("sidebar-open");
        if (sidebarBackdrop) {
            sidebarBackdrop.hidden = false;
        }
    }

    function closeSidebar() {
        if (!sidebar) {
            return;
        }
        document.body.classList.remove("sidebar-open");
        if (sidebarBackdrop) {
            sidebarBackdrop.hidden = true;
        }
    }

    function isDesktopSidebar() {
        return window.innerWidth > 1024;
    }

    function readSidebarCollapsedPreference() {
        try {
            return window.localStorage.getItem(sidebarCollapsedKey) === "true";
        } catch (error) {
            return false;
        }
    }

    function writeSidebarCollapsedPreference(isCollapsed) {
        try {
            window.localStorage.setItem(sidebarCollapsedKey, isCollapsed ? "true" : "false");
        } catch (error) {
            // Ignore storage restrictions.
        }
    }

    function syncSidebarCollapseButton(isCollapsed) {
        if (!sidebarCollapseToggle) {
            return;
        }

        const label = isCollapsed ? "Mostrar menu" : "Ocultar menu";
        const icon = sidebarCollapseToggle.querySelector("i");
        const text = sidebarCollapseToggle.querySelector("span");

        sidebarCollapseToggle.setAttribute("aria-label", label);
        sidebarCollapseToggle.setAttribute("title", label);
        if (icon) {
            icon.className = `fa-solid ${isCollapsed ? "fa-chevron-right" : "fa-chevron-left"}`;
        }
        if (text) {
            text.textContent = label;
        }
    }

    function setSidebarCollapsed(isCollapsed, persist = true) {
        document.body.classList.toggle("sidebar-collapsed", Boolean(isCollapsed));
        syncSidebarCollapseButton(Boolean(isCollapsed));
        if (persist) {
            writeSidebarCollapsedPreference(Boolean(isCollapsed));
        }
    }

    setSidebarCollapsed(readSidebarCollapsedPreference(), false);

    restoreScrollPosition();
    wireFlashMessages();
    renderLocalDateNodes();
    window.setInterval(renderLocalDateNodes, 60000);
    wireSplitWorkspace();

    document.addEventListener("click", (event) => {
        const closeFlashButton = event.target.closest("[data-flash-close]");
        if (closeFlashButton) {
            dismissFlash(closeFlashButton.closest("[data-flash]"));
            return;
        }

        if (event.target.closest("[data-sidebar-open]")) {
            openSidebar();
            return;
        }

        if (event.target.closest("[data-sidebar-close]") || event.target.closest("[data-sidebar-backdrop]")) {
            closeSidebar();
            return;
        }

        if (event.target.closest("[data-sidebar-collapse-toggle]")) {
            setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
            closeSidebar();
            return;
        }

        if (event.target.closest(".nav-link") && document.body.classList.contains("sidebar-open")) {
            closeSidebar();
            return;
        }

        const preserveLink = event.target.closest("a[data-preserve-scroll]");
        if (preserveLink?.href) {
            const targetUrl = new URL(preserveLink.href, window.location.origin);
            storeScrollPosition(
                `${targetUrl.pathname}${targetUrl.search}`,
                targetUrl.pathname
            );
        }

        const submitter = event.target.closest("button, input[type='submit']");
        if (!submitter) {
            return;
        }

        const form = submitter.form;
        if (!form) {
            return;
        }

        if (!submitter.hasAttribute("data-confirm-title")) {
            return;
        }

        event.preventDefault();

        if (form.hasAttribute("data-cancel-item-form")) {
            openCancelItemConfirm(
                form,
                submitter.getAttribute("data-confirm-title"),
                submitter.getAttribute("data-confirm-message")
            );
            return;
        }

        openConfirmModal(
            submitter.getAttribute("data-confirm-title"),
            submitter.getAttribute("data-confirm-message"),
            () => submitForm(form)
        );
    });

    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
            return;
        }

        if (form.hasAttribute("data-split-form")) {
            const validationError = validateSplitForm(form);
            if (validationError) {
                event.preventDefault();
                showLocalFlash(validationError, "error");
                return;
            }
        }

        if (form.hasAttribute("data-payment-form") && form.dataset.paymentConfirmed !== "true") {
            event.preventDefault();
            openPaymentModal(form);
            return;
        }

        if (form.hasAttribute("data-cancel-item-form")) {
            event.preventDefault();
            openCancelItemConfirm(
                form,
                form.getAttribute("data-confirm-title"),
                form.getAttribute("data-confirm-message")
            );
            return;
        }

        const title = form.getAttribute("data-confirm-title");
        if (title) {
            event.preventDefault();
            openConfirmModal(title, form.getAttribute("data-confirm-message"), () => submitForm(form));
            return;
        }

        if ((form.method || "get").toLowerCase() === "post") {
            storeScrollPosition();
        }
    });

    if (confirmAccept) {
        confirmAccept.addEventListener("click", () => {
            const action = pendingConfirmAction;
            if (action) {
                action();
            }
            closeConfirmModal();
        });
    }

    confirmCancelButtons.forEach((button) => {
        button.addEventListener("click", closeConfirmModal);
    });

    if (confirmQuantityInput) {
        confirmQuantityInput.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" || !confirmAccept) {
                return;
            }

            event.preventDefault();
            confirmAccept.click();
        });
    }

    if (paymentReceivedInput) {
        paymentReceivedInput.addEventListener("input", updatePaymentChange);
        paymentReceivedInput.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" || !paymentAccept || paymentAccept.disabled) {
                return;
            }

            event.preventDefault();
            paymentAccept.click();
        });
    }

    if (paymentAccept) {
        paymentAccept.addEventListener("click", () => {
            const form = pendingPaymentForm;
            if (!form || paymentAccept.disabled) {
                return;
            }

            writePaymentTenderFields(form);
            closePaymentModal();
            submitConfirmedPaymentForm(form);
        });
    }

    paymentCancelButtons.forEach((button) => {
        button.addEventListener("click", closePaymentModal);
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closeConfirmModal();
            closePaymentModal();
            closeSidebar();
        }
    });

    window.addEventListener("resize", () => {
        if (window.innerWidth > 1024) {
            closeSidebar();
        }
        if (!isDesktopSidebar()) {
            syncSidebarCollapseButton(document.body.classList.contains("sidebar-collapsed"));
        }
    });

    window.addEventListener("pageshow", () => {
        setFormSubmittingState(false);
        renderLocalDateNodes();
    });

    document.querySelectorAll(".toggle-password").forEach((button) => {
        button.addEventListener("click", () => {
            const field = button.parentElement?.querySelector("input");
            if (!field) {
                return;
            }

            const nextType = field.type === "password" ? "text" : "password";
            field.type = nextType;
            const icon = button.querySelector("i");
            if (icon) {
                icon.className = `fa-regular ${nextType === "password" ? "fa-eye" : "fa-eye-slash"}`;
            }
        });
    });

    const splitSelector = document.querySelector("[data-split-selector]");
    if (splitSelector) {
        splitSelector.addEventListener("change", () => {
            const orderUrl = splitSelector.getAttribute("data-order-url") || window.location.href;
            if (!orderUrl) {
                return;
            }

            const nextUrl = new URL(orderUrl, window.location.origin);
            nextUrl.searchParams.set("personas", splitSelector.value);
            nextUrl.searchParams.set("split", "1");
            storeScrollPosition(
                `${nextUrl.pathname}${nextUrl.search}`,
                nextUrl.pathname
            );
            window.location.assign(nextUrl.toString());
        });
    }

    const inventoryForm = document.querySelector("[data-inventory-form]");
    if (inventoryForm) {
        const productSelect = inventoryForm.querySelector("[data-inventory-product]");
        const typeSelect = inventoryForm.querySelector("[data-inventory-type]");
        const packagesInput = inventoryForm.querySelector("[data-inventory-packages]");
        const packagesLabel = inventoryForm.querySelector("[data-inventory-packages-label]");
        const packageUnitsInput = inventoryForm.querySelector("[data-inventory-package-units]");
        const quantityRow = inventoryForm.querySelector("[data-inventory-quantity-row]");
        const costRow = inventoryForm.querySelector("[data-inventory-cost-row]");
        const unitsInput = inventoryForm.querySelector("[data-inventory-units]");
        const unitsLabel = inventoryForm.querySelector("[data-inventory-units-label]");
        const priceLabel = inventoryForm.querySelector("[data-inventory-price-label]");
        const referencePriceInput = inventoryForm.querySelector("[data-inventory-reference-price]");
        const salePriceInput = inventoryForm.querySelector("[data-inventory-sale-price]");
        const salePriceLabel = inventoryForm.querySelector("[data-inventory-sale-price-label]");
        const inventorySummary = inventoryForm.querySelector("[data-inventory-summary]");
        const summaryLabel = inventoryForm.querySelector("[data-inventory-summary] span");
        const totalUnitsNode = inventoryForm.querySelector("[data-inventory-total-units]");
        const cashSummary = inventoryForm.querySelector("[data-inventory-cash-summary]");
        const cashSummaryLabel = inventoryForm.querySelector("[data-inventory-cash-summary] span");
        const cashTotalNode = inventoryForm.querySelector("[data-inventory-cash-total]");

        function numericValue(input) {
            const value = Number.parseFloat(input?.value || "0");
            return Number.isFinite(value) ? value : 0;
        }

        function selectedProductOption() {
            return productSelect?.selectedOptions?.[0] || null;
        }

        function loadProductDefaults() {
            const option = selectedProductOption();
            if (!option) {
                return;
            }

            if (packageUnitsInput) {
                packageUnitsInput.value = option.dataset.packageUnits || "1";
            }
            if (salePriceInput) {
                salePriceInput.value = option.dataset.salePrice || "";
            }
        }

        function setSectionVisible(section, visible, displayValue) {
            if (!section) {
                return;
            }

            section.hidden = !visible;
            section.style.display = visible ? displayValue : "none";
        }

        function syncInventorySummary() {
            const movementType = typeSelect?.value || "compra";
            const packages = Math.max(Math.trunc(numericValue(packagesInput)), 0);
            const packageUnits = Math.max(Math.trunc(numericValue(packageUnitsInput)), 1);
            const units = Math.trunc(numericValue(unitsInput));
            const referencePrice = Math.max(numericValue(referencePriceInput), 0);
            const salePrice = Math.max(numericValue(salePriceInput), 0);
            const purchaseUnits = (packages * packageUnits) + Math.max(units, 0);
            const saleUnits = (packages * packageUnits) + Math.abs(units);
            const totalUnits = movementType === "ajuste"
                ? 0
                : (movementType === "venta" ? saleUnits : purchaseUnits);
            const cashTotal = movementType === "compra"
                ? (packages * referencePrice) + (Math.max(units, 0) * (referencePrice / packageUnits))
                : (movementType === "venta" ? saleUnits * salePrice : 0);

            if (movementType === "compra") {
                setSectionVisible(quantityRow, true, "grid");
                setSectionVisible(costRow, true, "grid");
                setSectionVisible(inventorySummary, true, "flex");
                setSectionVisible(cashSummary, true, "flex");
                if (packagesLabel) {
                    packagesLabel.textContent = "Paquetes comprados";
                }
                if (packagesInput) {
                    packagesInput.disabled = false;
                }
                if (packageUnitsInput) {
                    packageUnitsInput.disabled = false;
                }
                if (unitsInput) {
                    unitsInput.disabled = false;
                    unitsInput.min = "0";
                }
                if (referencePriceInput) {
                    referencePriceInput.disabled = false;
                }
                if (unitsLabel) {
                    unitsLabel.textContent = "Unidades sueltas";
                }
                if (priceLabel) {
                    priceLabel.textContent = "Costo por paquete (1 paquete)";
                }
                if (referencePriceInput) {
                    referencePriceInput.placeholder = "Ej. 18.00";
                }
                if (salePriceLabel) {
                    salePriceLabel.textContent = "Precio de venta por unidad";
                }
                if (summaryLabel) {
                    summaryLabel.textContent = "Unidades a sumar";
                }
                if (cashSummaryLabel) {
                    cashSummaryLabel.textContent = "Egreso de caja";
                }
            } else if (movementType === "venta") {
                setSectionVisible(quantityRow, true, "grid");
                setSectionVisible(costRow, true, "grid");
                setSectionVisible(inventorySummary, true, "flex");
                setSectionVisible(cashSummary, true, "flex");
                if (packagesLabel) {
                    packagesLabel.textContent = "Paquetes a vender";
                }
                if (packagesInput) {
                    packagesInput.disabled = false;
                }
                if (packageUnitsInput) {
                    packageUnitsInput.disabled = false;
                }
                if (unitsInput) {
                    unitsInput.disabled = false;
                    unitsInput.min = "0";
                }
                if (referencePriceInput) {
                    referencePriceInput.disabled = false;
                }
                if (unitsLabel) {
                    unitsLabel.textContent = "Unidades a vender";
                }
                if (priceLabel) {
                    priceLabel.textContent = "Precio de referencia";
                }
                if (referencePriceInput) {
                    referencePriceInput.value = "";
                    referencePriceInput.placeholder = "No aplica";
                    referencePriceInput.disabled = true;
                }
                if (salePriceLabel) {
                    salePriceLabel.textContent = "Precio de venta por unidad";
                }
                if (summaryLabel) {
                    summaryLabel.textContent = "Unidades a vender";
                }
                if (cashSummaryLabel) {
                    cashSummaryLabel.textContent = "Ingreso de caja";
                }
            } else {
                setSectionVisible(quantityRow, false, "grid");
                setSectionVisible(costRow, false, "grid");
                setSectionVisible(inventorySummary, false, "flex");
                setSectionVisible(cashSummary, false, "flex");
                if (packagesLabel) {
                    packagesLabel.textContent = "Paquetes";
                }
                if (packagesInput) {
                    packagesInput.disabled = true;
                }
                if (packageUnitsInput) {
                    packageUnitsInput.disabled = true;
                }
                if (unitsInput) {
                    unitsInput.disabled = true;
                    unitsInput.value = "";
                    unitsInput.min = "0";
                }
                if (referencePriceInput) {
                    referencePriceInput.disabled = true;
                    referencePriceInput.value = "";
                    referencePriceInput.placeholder = "No aplica";
                }
                if (unitsLabel) {
                    unitsLabel.textContent = "Unidades";
                }
                if (priceLabel) {
                    priceLabel.textContent = "Costo";
                }
                if (salePriceLabel) {
                    salePriceLabel.textContent = "Precio de venta por unidad";
                }
                if (summaryLabel) {
                    summaryLabel.textContent = "Movimiento de stock";
                }
                if (cashSummaryLabel) {
                    cashSummaryLabel.textContent = "Caja";
                }
            }

            if (totalUnitsNode) {
                totalUnitsNode.textContent = totalUnits.toLocaleString(localDateLocale());
            }
            if (cashTotalNode) {
                cashTotalNode.textContent = formatMoney(cashTotal);
            }
        }

        productSelect?.addEventListener("change", () => {
            loadProductDefaults();
            syncInventorySummary();
        });
        typeSelect?.addEventListener("change", syncInventorySummary);
        [packagesInput, packageUnitsInput, unitsInput, referencePriceInput, salePriceInput].forEach((input) => {
            input?.addEventListener("input", syncInventorySummary);
        });

        loadProductDefaults();
        syncInventorySummary();
    }
})();
