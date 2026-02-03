#!/bin/bash
# Wrapper script to run visa checker with virtual environment

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Activate virtual environment (try both Unix and Windows paths)
if [ -f "$DIR/venv/bin/activate" ]; then
    source "$DIR/venv/bin/activate"
elif [ -f "$DIR/venv/Scripts/activate" ]; then
    source "$DIR/venv/Scripts/activate"
else
    echo "Warning: Virtual environment not found at $DIR/venv"
    echo "Running with system Python..."
fi

# Run the visa checker with all arguments
python "$DIR/visa_appointment_checker.py" "$@"

# Deactivate virtual environment if it was activated
if command -v deactivate &> /dev/null; then
    deactivate
fi
