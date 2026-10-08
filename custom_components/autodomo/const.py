"""Constantes da integracao AutoDomo."""

from __future__ import annotations

DOMAIN = "autodomo"

# Projeto Firebase da plataforma (ver AutoDomo/autodomo-cloud).
DEFAULT_PROJECT_ID = "autodomo-cloud"
DEFAULT_FUNCTIONS_URL = "https://us-central1-autodomo-cloud.cloudfunctions.net"
DEFAULT_DATABASE_URL = "https://autodomo-cloud-default-rtdb.firebaseio.com"
# Chave Web do Firebase e' publica por natureza (o app tambem a carrega);
# quem protege os dados sao as regras do RTDB. claimBridge devolve a chave
# atual, este e' so' o valor inicial.
DEFAULT_API_KEY = "AIzaSyBx-1cHyfhz56gnWmGnvC9SKD4UY3HBHBg"

# Dados guardados no config entry.
CONF_CODE = "code"
CONF_BRIDGE_ID = "bridge_id"
CONF_HOME_ID = "home_id"
CONF_PROJECT_ID = "project_id"
CONF_DATABASE_URL = "database_url"
CONF_FUNCTIONS_URL = "functions_url"
CONF_API_KEY = "api_key"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_AUTH_EMULATOR_HOST = "auth_emulator_host"

# Opcoes.
CONF_ENTITIES = "entities"

# Dominios do HA que a ponte sabe publicar (= kinds do contrato).
SUPPORTED_DOMAINS: tuple[str, ...] = (
    "light",
    "switch",
    "input_boolean",
    "cover",
    "sensor",
    "binary_sensor",
    "climate",
    "lock",
    "fan",
    "scene",
    "button",
)

STATE_DEBOUNCE_S = 0.3
HEARTBEAT_INTERVAL_S = 30
COMMAND_MAX_AGE_S = 30
STREAM_RETRY_MIN_S = 2
STREAM_RETRY_MAX_S = 60
