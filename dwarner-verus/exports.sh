# PORTS
export APP_VERUS_P2P_PORT="27485"
export APP_VERUS_RPC_PORT="27486"
export APP_VERUS_ELECTRUM_PORT="17485"

# RPC CREDENTIALS
export APP_VERUS_RPC_USER="umbrel"
export APP_VERUS_RPC_PASS="$(derive_entropy "app-${EXPORTS_APP_ID}-seed-rpc-pass")"
