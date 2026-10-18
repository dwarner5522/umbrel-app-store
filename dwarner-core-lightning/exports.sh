export APP_CORE_LIGHTNING_IP="10.21.21.94"
export APP_CORE_LIGHTNING_PORT="2103"
export APP_CORE_LIGHTNING_DAEMON_IP="10.21.21.96"
export APP_CORE_LIGHTNING_DAEMON_PORT="9736"
export APP_CORE_LIGHTNING_DAEMON_GRPC_PORT="2110"
export APP_CORE_LIGHTNING_WEBSOCKET_PORT="2106"
export APP_CORE_LIGHTNING_DATA_DIR="${EXPORTS_APP_DIR}/data/lightningd"
export CORE_LIGHTNING_REST_PORT="2107"

# The Bitcoin node is the one picked for the `bitcoin` dependency, which fills
# APP_BITCOIN_*. Only Bitcoin Knots and Knots (BLAKE2b) Companion will do, but the
# picker offers every app that implements `bitcoin`, so connect only when
# APP_BITCOIN_NODE_IP is one of those two nodes. Otherwise lightningd gets no
# address and stops at start rather than following another chain.
# `:-` throughout: umbrelOS sources this under `set -u`.
export APP_CORE_LIGHTNING_BITCOIN_NODE_IP=""
if [[ -n "${APP_BITCOIN_NODE_IP:-}" ]] && { [[ "${APP_BITCOIN_NODE_IP}" == "${APP_BITCOIN_KNOTS_NODE_IP:-}" ]] || [[ "${APP_BITCOIN_NODE_IP}" == "${APP_KNOTS_BLAKE2B_NODE_IP:-}" ]]; }; then
	export APP_CORE_LIGHTNING_BITCOIN_NODE_IP="${APP_BITCOIN_NODE_IP}"
else
	echo "Warning (${EXPORTS_APP_ID:-}): the selected Bitcoin node is not Bitcoin Knots or Knots (BLAKE2b) Companion; choose one of them in this app's settings" || true
fi

export APP_CORE_LIGHTNING_BITCOIN_NETWORK="${APP_BITCOIN_NETWORK:-mainnet}"
if [[ "${APP_CORE_LIGHTNING_BITCOIN_NETWORK}" == "mainnet" ]]; then
	export APP_CORE_LIGHTNING_BITCOIN_NETWORK="bitcoin"
fi

lightning_hidden_service_file="${EXPORTS_TOR_DATA_DIR}/app-${EXPORTS_APP_ID}-rest/hostname"
export APP_CORE_LIGHTNING_HIDDEN_SERVICE="$(cat "${lightning_hidden_service_file}" 2>/dev/null || echo "notyetset.onion")"

export APP_CONFIG_DIR="/data/app"
export APP_MODE="production"
export CORE_LIGHTNING_PATH="/root/.lightning"
export COMMANDO_CONFIG="/root/.lightning/.commando-env"
