from flask import Flask, request, render_template, redirect, url_for, flash
import configparser
import os

app = Flask(__name__)
app.secret_key = 'visa_checker_config_secret'

CONFIG_KEYS = [
    'EMAIL', 'PASSWORD', 'CURRENT_APPOINTMENT_DATE', 'LOCATION',
    'START_DATE', 'END_DATE', 'CHECK_FREQUENCY_MINUTES',
    'BURST_MODE_ENABLED', 'MULTI_LOCATION_CHECK', 'BACKUP_LOCATIONS',
    'PRIME_HOURS_START', 'PRIME_HOURS_END', 'PRIME_TIME_BACKOFF_MULTIPLIER',
    'WEEKEND_FREQUENCY_MULTIPLIER', 'PATTERN_LEARNING_ENABLED',
    'SMTP_SERVER', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS',
    'NOTIFY_EMAIL', 'AUTO_BOOK', 'DRIVER_RESTART_CHECKS', 
    'MAX_RETRY_ATTEMPTS', 'SLEEP_JITTER_SECONDS'
]

@app.route('/', methods=['GET', 'POST'])
def index():
    config = configparser.ConfigParser()
    
    # Create config.ini from template if it doesn't exist
    if not os.path.exists('config.ini'):
        if os.path.exists('config.ini.template'):
            config.read('config.ini.template')
            with open('config.ini', 'w') as f:
                config.write(f)
        else:
            # Create empty config file if template doesn't exist
            with open('config.ini', 'w') as f:
                f.write('[DEFAULT]\n')
    
    # Always read the config file
    config.read('config.ini')

    if request.method == 'POST':
        # DEFAULT section is automatically available in configparser
        for key in CONFIG_KEYS:
            if key in ['AUTO_BOOK', 'BURST_MODE_ENABLED', 'MULTI_LOCATION_CHECK', 'PATTERN_LEARNING_ENABLED']:
                value = 'True' if request.form.get(key) else 'False'
            else:
                value = request.form.get(key, '')
            config.set('DEFAULT', key, value)
        
        with open('config.ini', 'w') as f:
            config.write(f)
        
        flash('🚀 Strategic configuration saved successfully! Your optimization settings are now active.', 'success')
        return redirect(url_for('index'))

    # Set default values for strategic optimization settings
    defaults = {
        'CHECK_FREQUENCY_MINUTES': '3',
        'BURST_MODE_ENABLED': 'True',
        'MULTI_LOCATION_CHECK': 'True', 
        'BACKUP_LOCATIONS': 'Toronto,Montreal,Vancouver',
        'PRIME_HOURS_START': '6,12,17,22',
        'PRIME_HOURS_END': '9,14,19,1',
        'PRIME_TIME_BACKOFF_MULTIPLIER': '0.5',
        'WEEKEND_FREQUENCY_MULTIPLIER': '2.0',
        'PATTERN_LEARNING_ENABLED': 'True',
        'SMTP_SERVER': 'smtp.gmail.com',
        'SMTP_PORT': '587',
        'AUTO_BOOK': 'False',
        'DRIVER_RESTART_CHECKS': '50',
        'MAX_RETRY_ATTEMPTS': '2',
        'SLEEP_JITTER_SECONDS': '60'
    }
    
    current = {}
    for k in CONFIG_KEYS:
        try:
            # Try uppercase first, then lowercase for backward compatibility
            value = config.get('DEFAULT', k, fallback=None)
            if value is None:
                value = config.get('DEFAULT', k.lower(), fallback=defaults.get(k, ''))
            current[k] = value
        except Exception:
            current[k] = defaults.get(k, '')
    
    return render_template('index.html', current=current)

if __name__ == '__main__':
    import socket
    
    # Try to find an available port
    def find_free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port
    
    port = 5000
    try:
        app.run(debug=False, port=port, host='127.0.0.1')
    except OSError:
        port = find_free_port()
        print(f"Port 5000 is in use, trying port {port}")
        app.run(debug=False, port=port, host='127.0.0.1')