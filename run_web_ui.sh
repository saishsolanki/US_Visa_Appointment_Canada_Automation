#!/bin/bash
# Wrapper script to run web UI with virtual environment

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Activate virtual environment
source "$DIR/visa_env/bin/activate"

# Run the web UI
python "$DIR/web_ui.py"

# Deactivate virtual environment
deactivate
