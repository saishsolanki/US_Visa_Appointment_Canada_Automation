from flask import Flask, request, render_template
import configparser

app = Flask(__name__)

CONFIG_KEYS = [
    'EMAIL', 'PASSWORD', 'CURRENT_APPOINTMENT_DATE', 'LOCATION',
    'START_DATE', 'END_DATE', 'CHECK_FREQUENCY_MINUTES',
    'SMTP_SERVER', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS',
    'NOTIFY_EMAIL', 'AUTO_BOOK'
]

@app.route('/', methods=['GET', 'POST'])
def index():
    config = configparser.ConfigParser()
    config.read('config.ini')

    if request.method == 'POST':
        for key in CONFIG_KEYS:
            if key == 'AUTO_BOOK':
                value = 'True' if request.form.get(key) else 'False'
            else:
                value = request.form.get(key, '')
            config['DEFAULT'][key] = value
        with open('config.ini', 'w') as f:
            config.write(f)
        return "Configuration saved successfully!"

    current = {k: config['DEFAULT'].get(k, '') for k in CONFIG_KEYS}
    return render_template('index.html', current=current)

if __name__ == '__main__':
    app.run(debug=True)