$(function () {
    var POLL_MS = 5000;

    function MarketplaceLinkViewModel(parameters) {
        var self = this;
        self.settingsViewModel = parameters[0];

        self.testResult = ko.observable("");

        // ---- derived ----
        self.isConfigured = ko.pureComputed(function () {
            var s = self.settingsViewModel.settings;
            if (!s || !s.plugins || !s.plugins.marketplace_link) return false;
            return ((s.plugins.marketplace_link.webhook_url() || "").trim()) !== "";
        });

        self.lastDeliveryStatus = ko.pureComputed(function () {
            var s = self.settingsViewModel.settings;
            if (!s || !s.plugins || !s.plugins.marketplace_link) return 0;
            return parseInt(s.plugins.marketplace_link.last_delivery_status() || 0, 10);
        });

        self.lastDeliveryAt = ko.pureComputed(function () {
            var s = self.settingsViewModel.settings;
            if (!s || !s.plugins || !s.plugins.marketplace_link) return "";
            return s.plugins.marketplace_link.last_delivery_at() || "";
        });

        self.deliveryOk = ko.pureComputed(function () {
            var code = self.lastDeliveryStatus();
            return code >= 200 && code < 300;
        });

        self.deliveryLabel = ko.pureComputed(function () {
            if (!self.isConfigured()) return "Não configurado";
            if (!self.lastDeliveryAt()) return "Nenhum envio ainda";
            var code = self.lastDeliveryStatus();
            if (code === 0) return "Falhou (rede)";
            return "HTTP " + code;
        });

        self.deliveryClass = ko.pureComputed(function () {
            if (!self.isConfigured()) return "";
            if (!self.lastDeliveryAt()) return "";
            return self.deliveryOk() ? "label-success" : "label-important";
        });

        self.lastDeliveryAgo = ko.pureComputed(function () {
            var iso = self.lastDeliveryAt();
            if (!iso) return "";
            var when = new Date(iso);
            if (isNaN(when.getTime())) return "";
            var seconds = Math.max(0, Math.floor((Date.now() - when.getTime()) / 1000));
            if (seconds < 60) return "há " + seconds + "s";
            var minutes = Math.floor(seconds / 60);
            if (minutes < 60) return "há " + minutes + "min";
            var hours = Math.floor(minutes / 60);
            return "há " + hours + "h";
        });

        // ---- navbar bindings ----
        self.navbarLabel = ko.pureComputed(function () {
            if (!self.isConfigured()) return "Webhook";
            if (!self.lastDeliveryAt()) return "Webhook (idle)";
            return self.deliveryOk() ? "Webhook OK" : "Webhook erro";
        });

        self.navbarTooltip = ko.pureComputed(function () {
            if (!self.isConfigured()) {
                return "Webhook: não configurado. Abra Settings → Marketplace Link.";
            }
            if (!self.lastDeliveryAt()) {
                return "Webhook: configurado, sem envios ainda.";
            }
            var ago = self.lastDeliveryAgo();
            if (self.deliveryOk()) {
                return "Webhook: última entrega " + ago + " (HTTP " + self.lastDeliveryStatus() + ")";
            }
            var err = self.settingsViewModel.settings.plugins.marketplace_link.last_delivery_error() || "?";
            return "Webhook: falhou " + ago + " — " + err;
        });

        self.navbarIconClass = ko.pureComputed(function () {
            if (!self.isConfigured()) return "fa-link-slash";
            if (!self.lastDeliveryAt()) return "fa-cloud";
            return self.deliveryOk() ? "fa-cloud" : "fa-triangle-exclamation";
        });

        self.navbarStateClass = ko.pureComputed(function () {
            if (!self.isConfigured()) return "marketplace-unpaired";
            if (!self.lastDeliveryAt()) return "marketplace-connected";
            return self.deliveryOk() ? "marketplace-connected" : "marketplace-disconnected";
        });

        self.openSettings = function () {
            if (typeof self.settingsViewModel.show === "function") {
                self.settingsViewModel.show("settings_plugin_marketplace_link");
            }
        };

        // ---- API actions ----
        self.testWebhook = function () {
            self.testResult("...");
            OctoPrint.simpleApiCommand("marketplace_link", "test_webhook", {})
                .done(function (resp) {
                    if (resp.ok) {
                        self.testResult("OK (HTTP " + resp.status + ")");
                    } else if (resp.status) {
                        self.testResult("Falhou (HTTP " + resp.status + "): " + (resp.error || ""));
                    } else {
                        self.testResult("Falhou: " + (resp.error || "?"));
                    }
                })
                .fail(function (xhr) {
                    self.testResult("Falhou: " + xhr.statusText);
                });
        };

        // ---- live status poll (refreshes settings observables from the server) ----
        self._pollHandle = null;

        self.refreshStatus = function () {
            return self.settingsViewModel.requestData();
        };

        self.onStartupComplete = function () {
            if (self._pollHandle === null) {
                self._pollHandle = setInterval(self.refreshStatus, POLL_MS);
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: MarketplaceLinkViewModel,
        dependencies: ["settingsViewModel"],
        elements: [
            "#settings_plugin_marketplace_link",
            "#navbar_plugin_marketplace_link"
        ]
    });
});
