#!/usr/bin/env bash
# Start the cli-mcp-server.
#
# Three ways to bind, in the order they are checked:
#
#   LISTEN_FDS  systemd socket activation. The socket is created by systemd,
#               which is the only one of the three that can set its owner,
#               group and mode -- see the warning on UDS below. Preferred for
#               a unix-socket deployment.
#   UDS=path    uvicorn binds the unix socket itself.
#   HOST/PORT   TCP. The default, and the historical behaviour.
#
# THIS SERVER HAS NO AUTHENTICATION OF ITS OWN. A TCP bind is reachable by
# anything that can route to it. See the "Deployment" section of README.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate

if [[ -n "${LISTEN_FDS:-}" ]]; then
    # systemd hands the listening socket in as fd 3. Ownership and mode come
    # from the .socket unit (SocketUser=, SocketGroup=, SocketMode=), so a
    # 0660 socket is actually achievable here and nowhere else.
    exec uvicorn cli_mcp.server:app --fd 3 "$@"
fi

if [[ -n "${UDS:-}" ]]; then
    # WARNING: uvicorn chmods this socket to 0666 unconditionally -- see
    # `bind_socket` in uvicorn/config.py, where `uds_perms = 0o666` is a
    # literal with no setting in front of it. Any process on the host can then
    # connect, so a deployment relying on the socket's permissions as its
    # access boundary does not have one.
    #
    # Two ways out, and they compose:
    #   * Use socket activation instead (above), and let systemd set the mode.
    #   * Put the socket in a directory whose permissions do the work --
    #     e.g. /run/cli-mcp-server owned user:proxygroup mode 0750. Traversal
    #     is then denied regardless of the mode on the socket itself.
    exec uvicorn cli_mcp.server:app --uds "$UDS" "$@"
fi

exec uvicorn cli_mcp.server:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8100}" \
    "$@"
