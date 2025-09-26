#!/bin/bash
# Wrapper script to run visa checker with virtual environment

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Activate virtual environment
source "$DIR/visa_env/bin/activate"

# Run the visa checker with all arguments
python "$DIR/visa_appointment_checker.py" "$@"

# Deactivate virtual environment
deactivate
