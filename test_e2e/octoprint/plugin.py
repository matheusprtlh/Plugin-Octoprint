"""Stubs mínimos das classes mixin do OctoPrint que o plugin usa.

Não tentam emular o comportamento — só servem para o ``import octoprint.plugin``
funcionar e o herdeiro (``MarketplaceLinkPlugin``) instanciar. Em runtime real,
o OctoPrint injeta ``self._settings``, ``self._printer`` e ``self._logger``;
no harness, esses são injetados manualmente após instanciar o plugin.
"""


class _Mixin:
    pass


class SettingsPlugin(_Mixin):
    @staticmethod
    def on_settings_save(self, data):  # noqa: N805 — assinatura imita OctoPrint
        # No OctoPrint real isso retorna o resultado de salvar; aqui ignoramos.
        return None


class AssetPlugin(_Mixin):
    pass


class TemplatePlugin(_Mixin):
    pass


class StartupPlugin(_Mixin):
    pass


class ShutdownPlugin(_Mixin):
    pass


class SimpleApiPlugin(_Mixin):
    pass


class EventHandlerPlugin(_Mixin):
    pass
