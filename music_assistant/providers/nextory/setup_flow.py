"""Setup flow for the Nextory provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from nextory import NextoryClient
from nextory.exceptions import NextoryApiError
from nextory.helpers import generate_device_id

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.nextory.constants import (
    CONF_DEVICE_ID,
    CONF_LANGUAGE,
    CONF_LOGIN_KEY,
    CONF_LOGIN_TOKEN,
    CONF_PASSWORD,
    CONF_PROFILE_TOKEN,
    CONF_USERNAME,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# Nextory returns bare language codes; only label the codes we can name.
_LANGUAGE_NAMES = {
    "sv": "Svenska",
    "en": "English",
    "fi": "Suomi",
    "da": "Dansk",
    "nb": "Norsk",
    "de": "Deutsch",
    "nl": "Nederlands",
    "es": "Español",
    "fr": "Français",
    "it": "Italiano",
    "ar": "العربية",
}

_LOGIN_ENTRIES = [
    ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True),
]


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: log in, pick a profile and language, then finish."""
    # Nextory binds tokens to the device id they were issued on, so one id is used for
    # login, profile selection and every request the provider makes afterwards. A
    # unique id per instance also keeps profiles of one account from evicting each other.
    device_id = generate_device_id()
    login_token, username, password = await _login_step(session, device_id)
    await _profile_step(session, device_id, login_token, username, password)


async def _login_step(session: SetupSession, device_id: str) -> tuple[str, str, str]:
    """Collect credentials and return a Nextory login token with the credentials."""
    errors: dict[str, str] | None = None
    while True:
        submitted = await session.form(_LOGIN_ENTRIES, step_id="user", errors=errors)
        username = cast("str", submitted[CONF_USERNAME])
        password = cast("str", submitted[CONF_PASSWORD])
        async with NextoryClient(device_id=device_id) as client:
            try:
                return cast("str", await client.login(username, password)), username, password
            except NextoryApiError as err:
                errors = {"base": err.description or str(err)}


async def _profile_step(
    session: SetupSession, device_id: str, login_token: str, username: str, password: str
) -> None:
    """Collect the profile/language selection and finish the flow."""
    async with NextoryClient(login_token=login_token, device_id=device_id) as client:
        profiles = await client.get_profiles()
        language_options, default_language = await _fetch_languages(client)

    profile_options = [ConfigValueOption(p.login_key, p.name) for p in profiles.profiles]
    default_login_key = profiles.profiles[0].login_key if len(profiles.profiles) == 1 else None
    entries = [
        ConfigEntry(
            key=CONF_LOGIN_KEY,
            type=ConfigEntryType.STRING,
            required=True,
            options=profile_options,
            default_value=default_login_key,
        ),
        ConfigEntry(
            key=CONF_LANGUAGE,
            type=ConfigEntryType.STRING,
            required=True,
            options=language_options,
            default_value=default_language,
        ),
    ]

    errors: dict[str, str] | None = None
    while True:
        submitted = await session.form(entries, step_id="profile", errors=errors, last_step=True)
        login_key = cast("str", submitted[CONF_LOGIN_KEY])
        language = cast("str", submitted[CONF_LANGUAGE])
        async with NextoryClient(login_token=login_token, device_id=device_id) as client:
            try:
                profile_token = await client.select_profile(login_key)
            except NextoryApiError as err:
                errors = {"base": err.description or str(err)}
                continue
        try:
            await session.finish(
                {
                    CONF_LOGIN_TOKEN: login_token,
                    CONF_LOGIN_KEY: login_key,
                    CONF_PROFILE_TOKEN: profile_token,
                    CONF_LANGUAGE: language,
                    CONF_DEVICE_ID: device_id,
                    # kept so the client can sign in again if Nextory rejects the token
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
            )
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _fetch_languages(client: NextoryClient) -> tuple[list[ConfigValueOption], str]:
    """Return the account's available language options and its default language."""
    try:
        account = await client.get_account()
        text = await client._request("GET", f"{client._base_url}/user/v1.1/markets")
        for market in json.loads(text):
            if market["country_code"] != account.country:
                continue
            options = [
                ConfigValueOption(lang, _LANGUAGE_NAMES.get(lang, lang))
                for lang in market["allowed_languages"]
            ]
            return options, market["primary_languages"][0]
    except Exception:  # noqa: S110 - language options are a non-essential nicety
        pass
    return [], "en"
