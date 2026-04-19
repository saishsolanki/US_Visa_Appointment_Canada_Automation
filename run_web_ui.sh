#!/bin/bash
# Wrapper script to run web UI with virtual environment

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

bootstrap_env() {
    local host_python=""
    if command -v python3 &> /dev/null; then
        host_python="python3"
    elif command -v python &> /dev/null; then
        host_python="python"
    fi

    if [[ -z "$host_python" ]]; then
        echo "Error: No Python interpreter found for bootstrap (python3/python)."
        exit 127
    fi

    echo "[run_web_ui] Virtual environment missing or unhealthy. Bootstrapping..."
    "$host_python" "$DIR/bootstrap_env.py" --venv-dir venv --fresh
}

# Activate virtual environment (try both Unix and Windows paths)
if [ -f "$DIR/venv/bin/activate" ]; then
    source "$DIR/venv/bin/activate"
elif [ -f "$DIR/venv/Scripts/activate" ]; then
    source "$DIR/venv/Scripts/activate"
else
    bootstrap_env
    source "$DIR/venv/bin/activate"
fi

# Resolve Python interpreter robustly
if [ -x "$DIR/venv/bin/python" ]; then
    PYTHON_BIN="$DIR/venv/bin/python"
    "$PYTHON_BIN" -m pip --version &> /dev/null || {
        bootstrap_env
        source "$DIR/venv/bin/activate"
        PYTHON_BIN="$DIR/venv/bin/python"
    }
elif command -v python3 &> /dev/null; then
    PYTHON_BIN="python3"
elif command -v python &> /dev/null; then
    PYTHON_BIN="python"
else
    echo "Error: No Python interpreter found (python3/python)."
    exit 127
fi

# Run the web UI
"$PYTHON_BIN" "$DIR/web_ui.py"

# Deactivate virtual environment if it was activated
if command -v deactivate &> /dev/null; then
    deactivate
fi
