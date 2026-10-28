"""Adding JPlay devices: players (also found by themselves on the network) and JPlaying Now displays."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:  # Home Assistant 2025.1 and later
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # older
    from homeassistant.components.zeroconf import ZeroconfServiceInfo  # type: ignore[no-redef]

from .api import ApiClient, JPlayError
from .const import CONF_KIND, DOMAIN, HUB_PORT, KIND_HUB, KIND_NOW, KIND_PLAYER, NOW_PORT, PLAYER_PORT


class JPlayConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    def _api(self, base: str) -> ApiClient:
        return ApiClient(async_get_clientsession(self.hass), base)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[KIND_PLAYER, KIND_NOW, KIND_HUB])

    # ---------------------------------------------------------------- JPlay player
    async def _player_entry(self, host: str, port: int, errors: dict[str, str]) -> ConfigFlowResult | None:
        try:
            info = await self._api(f"http://{host}:{port}").get("/api/v1/info")
        except JPlayError:
            errors["base"] = "cannot_connect"
            return None
        await self.async_set_unique_id(info["id"])
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        return self.async_create_entry(title=info.get("name") or host,
                                       data={CONF_KIND: KIND_PLAYER, CONF_HOST: host, CONF_PORT: port})

    async def async_step_player(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._player_entry(user_input[CONF_HOST].strip(), user_input[CONF_PORT], errors)
            if result:
                return result
        schema = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PORT, default=PLAYER_PORT): int})
        return self.async_show_form(step_id="player", data_schema=self.add_suggested_values_to_schema(schema, user_input),
                                    errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        if discovery_info.type.startswith("_jplay-display."):
            return await self._display_found(discovery_info)
        if discovery_info.type.startswith("_jplay-hub."):
            return await self._hub_found(discovery_info)
        host, port = str(discovery_info.ip_address), discovery_info.port or PLAYER_PORT
        pid = discovery_info.properties.get("id")
        if pid:
            await self.async_set_unique_id(pid)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        try:
            info = await self._api(f"http://{host}:{port}").get("/api/v1/info")
        except JPlayError:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(info["id"])
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._discovered = {CONF_HOST: host, CONF_PORT: port, CONF_NAME: info.get("name") or host}
        self.context["title_placeholders"] = {"name": self._discovered[CONF_NAME]}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        d = self._discovered
        if user_input is not None:
            return self.async_create_entry(title=d[CONF_NAME],
                                           data={CONF_KIND: d.get(CONF_KIND, KIND_PLAYER), CONF_HOST: d[CONF_HOST],
                                                 CONF_PORT: d[CONF_PORT]})
        self._set_confirm_only()
        kind = d.get(CONF_KIND, KIND_PLAYER)
        return self.async_show_form(step_id={KIND_PLAYER: "zeroconf_confirm", KIND_HUB: "zeroconf_confirm_hub"}.get(
                                        kind, "zeroconf_confirm_display"),
                                    description_placeholders={"name": d[CONF_NAME], "host": d[CONF_HOST]})

    async def async_step_zeroconf_confirm_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_zeroconf_confirm(user_input)

    async def _hub_found(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        host, port = str(discovery_info.ip_address), discovery_info.port or HUB_PORT
        hid = discovery_info.properties.get("id")
        if hid:
            await self.async_set_unique_id(f"hub-{hid}")
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        try:
            info = await self._api(f"http://{host}:{port}").get("/api/info")
        except JPlayError:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(f"hub-{info['id']}")
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._discovered = {CONF_KIND: KIND_HUB, CONF_HOST: host, CONF_PORT: port, CONF_NAME: "JPlay hub"}
        self.context["title_placeholders"] = {"name": "JPlay hub"}
        return await self.async_step_zeroconf_confirm()

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            try:
                info = await self._api(f"http://{host}:{port}").get("/api/info")
                if info.get("kind") != "hub":
                    raise JPlayError("not a hub")
            except (JPlayError, AttributeError):
                errors["base"] = "cannot_connect_hub"
            else:
                await self.async_set_unique_id(f"hub-{info['id']}")
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                return self.async_create_entry(title="JPlay hub", data={CONF_KIND: KIND_HUB, CONF_HOST: host, CONF_PORT: port})
        schema = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PORT, default=HUB_PORT): int})
        return self.async_show_form(step_id="hub", errors=errors,
                                    data_schema=self.add_suggested_values_to_schema(schema, user_input))

    async def async_step_zeroconf_confirm_display(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_zeroconf_confirm(user_input)

    async def _display_found(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A JPlaying Now display announced itself (version 2 and later)."""
        host, port = str(discovery_info.ip_address), discovery_info.port or NOW_PORT
        for entry in self._async_current_entries():          # added by hand before it announced itself
            if entry.data.get(CONF_KIND) == KIND_NOW and entry.data.get(CONF_HOST) in (host, discovery_info.hostname.rstrip(".")):
                return self.async_abort(reason="already_configured")
        try:
            info = await self._api(f"http://{host}:{port}").get("/api/info")
        except JPlayError:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(f"display-{info['id']}")
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._discovered = {CONF_KIND: KIND_NOW, CONF_HOST: host, CONF_PORT: port, CONF_NAME: info.get("name") or host}
        self.context["title_placeholders"] = {"name": self._discovered[CONF_NAME]}
        return await self.async_step_zeroconf_confirm()

    # ---------------------------------------------------------------- JPlaying Now
    async def async_step_jplayingnow(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            api = self._api(f"http://{host}:{port}")
            try:
                await api.get("/api/screen")
            except JPlayError:
                errors["base"] = "cannot_connect_now"
            else:
                try:
                    unique = f"display-{(await api.get('/api/info'))['id']}"    # version 2 and later
                except (JPlayError, KeyError, TypeError):
                    unique = f"jplayingnow-{host}:{port}"
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME].strip() or f"JPlaying Now {host}",
                                               data={CONF_KIND: KIND_NOW, CONF_HOST: host, CONF_PORT: port})
        schema = vol.Schema({
            vol.Required(CONF_NAME, default="JPlaying Now"): str,
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT, default=NOW_PORT): int,
        })
        return self.async_show_form(step_id="jplayingnow", errors=errors,
                                    data_schema=self.add_suggested_values_to_schema(schema, user_input))
