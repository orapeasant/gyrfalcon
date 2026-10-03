#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-}"
PROFILE_HOME="${GYRFALCON_HOME:-$HOME/.gyrfalcon}"
RUN_DIR="$PROFILE_HOME/run"
LOG_DIR="$PROFILE_HOME/logs"

if [[ "$ACTION" != start && "$ACTION" != stop ]]; then
    echo "Usage: $0 {start|stop}" >&2
    exit 2
fi

if [[ "$ACTION" == start ]]; then
    if ! command -v setsid >/dev/null 2>&1; then
        echo "startall requires setsid (usually provided by util-linux)." >&2
        exit 1
    fi
    if [[ -x "$ROOT/.venv/bin/gyrfalcon" ]]; then
        CLI=("$ROOT/.venv/bin/gyrfalcon")
        PYTHON=("$ROOT/.venv/bin/python")
    elif command -v uv >/dev/null 2>&1; then
        CLI=(uv run --project "$ROOT" gyrfalcon)
        PYTHON=(uv run --project "$ROOT" python)
    else
        echo "Could not find $ROOT/.venv/bin/gyrfalcon or uv. Run 'uv sync' first." >&2
        exit 1
    fi
    mkdir -p "$RUN_DIR" "$LOG_DIR"
    chmod 700 "$RUN_DIR"
fi

start_service() {
    local name="$1" subcommand="$2" pidfile="$RUN_DIR/$1.pid" log="$LOG_DIR/$1.out.log"
    local pid args

    if [[ -f "$pidfile" ]]; then
        pid="$(cat "$pidfile")"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 -- "-$pid" 2>/dev/null; then
            args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
            if [[ "$args" == *gyrfalcon*"$subcommand"* ]]; then
                echo "$name already running (PID $pid)"
                return
            fi
            echo "Refusing to start $name: its recorded process group $pid is still active." >&2
            echo "Inspect it and remove $pidfile if it is stale." >&2
            return 1
        fi
        rm -f "$pidfile"
    fi

    nohup setsid "${CLI[@]}" "$subcommand" >>"$log" 2>&1 </dev/null &
    pid=$!
    printf '%s\n' "$pid" > "$pidfile"
    sleep 1
    if ! kill -0 -- "-$pid" 2>/dev/null; then
        rm -f "$pidfile"
        echo "Failed to start $name; see $log" >&2
        tail -n 20 "$log" >&2 || true
        return 1
    fi
    echo "Started $name (PID $pid); log: $log"
}

print_endpoints() {
    local dashboard_url
    dashboard_url="$("${PYTHON[@]}" -c '
from gyrfalcon.gyrfalcon_constants import load_repo_root_env_file
from gyrfalcon.config import load_env_file, load_config
load_env_file()
load_repo_root_env_file()
web = load_config().get("web", {})
host, port = str(web.get("host", "127.0.0.1")), web.get("port", 9119)
host = f"[{host}]" if ":" in host and not host.startswith("[") else host
print(f"http://{host}:{port}")
' 2>/dev/null)" || dashboard_url="http://127.0.0.1:9119"
    echo
    echo "Dashboard URL: $dashboard_url"
    echo "Dashboard listening port: ${dashboard_url##*:}"
    echo "Gateway and flow daemon do not use a fixed HTTP port; gateway adapter ports come from config.yaml."
}

stop_service() {
    local name="$1" pidfile="$RUN_DIR/$1.pid" pid i
    if [[ ! -f "$pidfile" ]]; then
        echo "$name is not recorded as running"
        return
    fi
    pid="$(cat "$pidfile")"
    if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
        echo "Ignoring invalid PID file: $pidfile" >&2
        rm -f "$pidfile"
        return
    fi
    if ! kill -0 -- "-$pid" 2>/dev/null; then
        echo "$name is already stopped (removing stale PID file)"
        rm -f "$pidfile"
        return
    fi
    echo "Stopping $name (PID $pid)"
    kill -TERM -- "-$pid" 2>/dev/null || true
    for i in {1..10}; do
        if ! kill -0 -- "-$pid" 2>/dev/null; then
            rm -f "$pidfile"
            echo "$name stopped"
            return
        fi
        sleep 1
    done
    echo "$name did not stop in 10 seconds; sending SIGKILL"
    kill -KILL -- "-$pid" 2>/dev/null || true
    rm -f "$pidfile"
}

if [[ "$ACTION" == start ]]; then
    start_service dashboard dashboard
    start_service gateway gateway
    start_service flow-daemon flow-daemon
    print_endpoints
else
    # Stop the flow worker and gateway before the dashboard they depend on.
    stop_service flow-daemon
    stop_service gateway
    stop_service dashboard
fi
