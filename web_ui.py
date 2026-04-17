from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, Response, stream_with_context
import os
import subprocess
import threading
import time

from config_manager import BOOLEAN_KEYS, CONFIG_KEYS, ConfigManager

app = Flask(__name__)
app.secret_key = os.urandom(32)

# Path to the log file produced by logging_utils.py
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "visa_checker.log")

# In-memory ring-buffer for update output (shared between threads)
_update_output: list[str] = []
_update_lock = threading.Lock()
_update_running = False

CONFIG_MANAGER = ConfigManager()

@app.route('/', methods=['GET', 'POST'])
def index():
    CONFIG_MANAGER.load_parser()

    if request.method == 'POST':
        updates = {}
        for key in CONFIG_KEYS:
            if key in BOOLEAN_KEYS:
                value = 'True' if request.form.get(key) else 'False'
            else:
                value = request.form.get(key, '')
            updates[key] = value

        CONFIG_MANAGER.save_updates(updates)
        
        flash('🚀 Strategic configuration saved successfully! Your optimization settings are now active.', 'success')
        return redirect(url_for('index'))

    current = CONFIG_MANAGER.ui_values()
    
    return render_template('index.html', current=current)


@app.route('/analytics')
def analytics():
    """Web dashboard showing slot ledger analytics."""
    try:
        from slot_ledger import SlotLedger
        ledger = SlotLedger()
        stats = ledger.analytics_summary()
        recent = ledger.recent_slots(limit=50)
    except Exception:
        stats = {}
        recent = []

    total = stats.get("total_slots", 0)
    unique_dates = stats.get("unique_dates", 0)
    locations = stats.get("locations", 0)
    booked = stats.get("booked", 0)
    notified = stats.get("notified", 0)

    rows_html = ""
    for slot in recent:
        status = ""
        if slot.get("booked"):
            status = "&#x2705; Booked"
        elif slot.get("notified"):
            status = "&#x1F514; Notified"
        else:
            status = "&#x1F7E2; Seen"
        rows_html += (
            f"<tr><td>{slot.get('slot_date','')}</td>"
            f"<td>{slot.get('location','')}</td>"
            f"<td>{slot.get('discovered','')[:19]}</td>"
            f"<td>{status}</td></tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>Slot Analytics</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       max-width: 960px; margin: 2rem auto; padding: 0 1rem; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
          gap: 1rem; margin-bottom: 2rem; }}
.stat {{ background: #161b22; padding: 1.2rem; border-radius: 8px; text-align: center;
         border: 1px solid #30363d; }}
.stat .value {{ font-size: 2rem; font-weight: bold; color: #58a6ff; }}
.stat .label {{ font-size: 0.85rem; color: #8b949e; margin-top: 0.3rem; }}
table {{ width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px;
         overflow: hidden; border: 1px solid #30363d; }}
th, td {{ padding: 0.6rem 0.8rem; text-align: left; border-bottom: 1px solid #21262d; }}
th {{ background: #0d1117; color: #58a6ff; font-weight: 600; }}
a {{ color: #58a6ff; }}
</style>
</head>
<body>
<h1>&#x1F4CA; Slot Analytics Dashboard</h1>
<p><a href=\"/\">&larr; Back to Config</a></p>
<div class=\"stats\">
  <div class=\"stat\"><div class=\"value\">{total}</div><div class=\"label\">Total Slots</div></div>
  <div class=\"stat\"><div class=\"value\">{unique_dates}</div><div class=\"label\">Unique Dates</div></div>
  <div class=\"stat\"><div class=\"value\">{locations}</div><div class=\"label\">Locations</div></div>
  <div class=\"stat\"><div class=\"value\">{booked}</div><div class=\"label\">Booked</div></div>
  <div class=\"stat\"><div class=\"value\">{notified}</div><div class=\"label\">Notified</div></div>
</div>
<h2>Recent Slots</h2>
<table><thead><tr><th>Date</th><th>Location</th><th>Discovered</th><th>Status</th></tr></thead>
<tbody>{rows_html if rows_html else '<tr><td colspan=\"4\">No slots recorded yet</td></tr>'}
</tbody></table>
</body></html>"""
    return html


@app.route('/logs')
def logs():
    """Live log viewer page."""
    return render_template('logs.html')


@app.route('/stream/logs')
def stream_logs():
    """Server-Sent Events endpoint that tails the log file."""
    def generate():
        # Send the last 200 lines of the log file first (catch-up)
        try:
            if os.path.exists(_LOG_PATH):
                with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                catchup = lines[-200:] if len(lines) > 200 else lines
                for line in catchup:
                    yield f"data: {line.rstrip()}\n\n"
        except OSError:
            yield "data: [log file not found – start the checker to generate logs]\n\n"

        # Now tail for new lines
        try:
            with open(_LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(0, 2)  # seek to end
                idle_ticks = 0
                while True:
                    line = f.readline()
                    if line:
                        idle_ticks = 0
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        time.sleep(0.5)
                        idle_ticks += 1
                        # Send a keep-alive comment every 30 seconds of inactivity
                        if idle_ticks >= 60:
                            idle_ticks = 0
                            yield ": keep-alive\n\n"
        except OSError:
            yield "data: [log file disappeared]\n\n"

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/update', methods=['GET'])
def update_page():
    """Web-based update page."""
    return render_template('update.html')


@app.route('/api/update', methods=['POST'])
def api_update():
    """Trigger a git pull + pip install and stream output back as plain text."""
    global _update_running

    with _update_lock:
        if _update_running:
            return jsonify({'error': 'An update is already in progress'}), 409
        _update_running = True
        _update_output.clear()

    def run_update():
        global _update_running
        project_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            _append_output("🔄 Starting update...\n")
            # git pull
            result = subprocess.run(
                ['git', 'pull', 'origin', 'main'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            _append_output(result.stdout or '')
            if result.stderr:
                _append_output(result.stderr)
            if result.returncode != 0:
                _append_output(f"⚠️  git pull exited with code {result.returncode}\n")
                return

            # pip install
            _append_output("\n📦 Updating Python dependencies...\n")
            pip_cmd = ['pip', 'install', '--upgrade', '-r', 'requirements.txt']
            result = subprocess.run(
                pip_cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            _append_output(result.stdout or '')
            if result.stderr:
                _append_output(result.stderr)
            if result.returncode == 0:
                _append_output("\n✅ Update complete! Restart the checker for changes to take effect.\n")
            else:
                _append_output(f"\n⚠️  pip install exited with code {result.returncode}\n")
        except Exception as exc:
            _append_output(f"\n❌ Error during update: {exc}\n")
        finally:
            with _update_lock:
                _update_running = False

    thread = threading.Thread(target=run_update, daemon=True)
    thread.start()
    return jsonify({'status': 'started'})


@app.route('/api/update/status')
def api_update_status():
    """Return current update output and running state."""
    with _update_lock:
        running = _update_running
        output = list(_update_output)
    return jsonify({'running': running, 'output': output})


def _append_output(text: str) -> None:
    with _update_lock:
        _update_output.append(text)
        # Keep buffer bounded
        if len(_update_output) > 500:
            _update_output.pop(0)


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
