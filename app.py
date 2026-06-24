from flask import Flask, render_template, request, jsonify, session
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import re
import os
from gspread.utils import rowcol_to_a1

app = Flask(__name__)
app.secret_key = os.urandom(24)  # For session management

# --- Find credentials.json ---
def find_credentials():
    possible_paths = [
        "credentials.json",
        "/etc/secrets/credentials.json",
        os.path.join(os.path.dirname(__file__), "credentials.json"),
        os.path.join(os.getcwd(), "credentials.json"),
    ]
    for path in possible_paths:
        if os.path.exists(path):
            print(f"Found credentials at: {path}")
            return path
    env_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if env_json:
        print("Using credentials from environment variable")
        temp_path = "/tmp/credentials.json"
        with open(temp_path, "w") as f:
            f.write(env_json)
        return temp_path
    raise FileNotFoundError("Could not find credentials.json anywhere")

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_path = find_credentials()
creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
client = gspread.authorize(creds)

SHEET_ID = "1U3VIktKv4S0w5SUx6IE2XIhiV63f9YA1s6CxSOBtNeg"
spreadsheet = client.open_by_key(SHEET_ID)

# --- Get or create sheets ---
try:
    sheet = spreadsheet.worksheet("Borrow Log")
except gspread.exceptions.WorksheetNotFound:
    try:
        sheet = spreadsheet.worksheet("Sheet1")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.sheet1

try:
    status_sheet = spreadsheet.worksheet(" Battery Status")
except gspread.exceptions.WorksheetNotFound:
    try:
        status_sheet = spreadsheet.worksheet("Battery Status")
    except gspread.exceptions.WorksheetNotFound:
        status_sheet = spreadsheet.add_worksheet(title=" Battery Status", rows="100", cols="3")
        status_sheet.update('A1:C1', [['Battery ID', 'Status', 'Holder']])

try:
    inventory_sheet = spreadsheet.worksheet("Battery Inventory")
except gspread.exceptions.WorksheetNotFound:
    inventory_sheet = spreadsheet.add_worksheet(title="Battery Inventory", rows="100", cols="6")
    inventory_sheet.update('A1:F1', [['Battery ID', 'Brand', 'Type', 'Capacity (mAh)', 'Voltage (V)', 'Cells']])

try:
    voltage_sheet = spreadsheet.worksheet("Cell Voltages")
except gspread.exceptions.WorksheetNotFound:
    voltage_sheet = spreadsheet.add_worksheet(title="Cell Voltages", rows="1000", cols="20")
    voltage_sheet.update('A1:O1', [['Timestamp', 'Battery ID', 'Name', 'UserType', 'Action', 'Quantity',
                                    'Cell 1', 'Cell 2', 'Cell 3', 'Cell 4', 'Cell 5', 'Cell 6',
                                    'Min Voltage', 'Max Voltage', 'Difference', 'Condition', 'Suggestion']])

def safe_append_row(worksheet, row_values):
    """Append a row anchored to an explicit A1 range instead of relying on
    Google Sheets' table auto-detection, which can drift columns over time."""
    next_row = len(worksheet.col_values(1)) + 1
    start_cell = rowcol_to_a1(next_row, 1)
    end_cell = rowcol_to_a1(next_row, len(row_values))
    worksheet.update(f'{start_cell}:{end_cell}', [row_values], value_input_option='USER_ENTERED')

def parse_cells(cells_str):
    if not cells_str:
        return 3
    match = re.search(r'(\d+)', str(cells_str))
    return int(match.group(1)) if match else 3

def get_cell_count_from_cells(cells_str):
    """Extract number from cells string like '3S' -> 3, '4S' -> 4"""
    if not cells_str:
        return 3
    match = re.search(r'(\d+)', str(cells_str))
    return int(match.group(1)) if match else 3

def is_battery_restricted_for_student(cells_str):
    """Check if battery is 4S or higher"""
    if not cells_str:
        return False
    match = re.search(r'(\d+)', str(cells_str))
    if match:
        cell_count = int(match.group(1))
        return cell_count >= 4
    return False

def get_battery_suggestion(condition, avg_v, action):
    """Get action suggestion based on condition and action"""
    if condition.startswith('Danger'):
        return "🚨 DO NOT CHARGE! Report this battery to the lab technician immediately."
    elif condition == 'Warning - Low Voltage':
        return "⚡ Charge to storage voltage (3.80-3.85V) before storing."
    elif condition == 'Warning - Very Low Voltage':
        return "⚠️ Charge immediately! Battery is at critical level."
    elif condition == 'Warning - Slightly Unbalanced':
        return "🔄 Balance charge recommended before next use."
    elif condition == 'Good - Storage Voltage':
        return "✅ Ready for storage. Place battery in the designated storage area."
    elif condition == 'Good - Full Charge':
        return "✅ Battery is full. You may store it or use it."
    else:
        return "✅ Battery is in good condition."

def update_battery_status(battery_id, action, name):
    try:
        status_ids = status_sheet.col_values(1)
        row_number = None
        for i, bid in enumerate(status_ids, start=1):
            if bid.strip() == battery_id:
                row_number = i
                break
        
        if row_number:
            if action == "Borrow":
                status_sheet.update_cell(row_number, 2, "Borrowed")
                status_sheet.update_cell(row_number, 3, name)
            elif action == "Return":
                status_sheet.update_cell(row_number, 2, "Available")
                status_sheet.update_cell(row_number, 3, "")
        else:
            new_row = [battery_id, "Borrowed" if action == "Borrow" else "Available", name if action == "Borrow" else ""]
            safe_append_row(status_sheet, new_row)
    except Exception as e:
        print(f"Status update error: {e}")

@app.route('/api/battery/<battery_id>')
def api_battery_lookup(battery_id):
    """API endpoint to look up battery specs"""
    try:
        # Normalize battery ID
        if battery_id.isdigit():
            search_id = f"LiPo{battery_id.zfill(3)}"
        else:
            search_id = battery_id
        
        battery_ids = inventory_sheet.col_values(1)
        for idx, bid in enumerate(battery_ids, start=1):
            if bid.strip() == search_id:
                row = inventory_sheet.row_values(idx)
                return jsonify({
                    'found': True,
                    'battery_id': bid,
                    'brand': row[1] if len(row) > 1 else '',
                    'type': row[2] if len(row) > 2 else '',
                    'capacity': row[3] if len(row) > 3 else '',
                    'voltage': row[4] if len(row) > 4 else '',
                    'cells': row[5] if len(row) > 5 else '3S'
                })
        
        return jsonify({'found': False})
    except Exception as e:
        print(f"API error: {e}")
        return jsonify({'found': False, 'error': str(e)})

@app.route('/api/check_password', methods=['POST'])
def check_password():
    """Verify the lab password"""
    data = request.get_json()
    password = data.get('password', '')
    if password == 'Lab-07':
        session['authenticated'] = True
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Incorrect password. Please try again.'})

@app.route('/api/check_access', methods=['POST'])
def check_access():
    """Check if user (student/staff) can borrow a battery"""
    data = request.get_json()
    battery_id = data.get('battery_id', '')
    user_type = data.get('user_type', '')
    
    # If staff, always allowed
    if user_type == 'Staff':
        return jsonify({'allowed': True})
    
    # If student, check battery cells
    try:
        # Normalize battery ID
        if battery_id.isdigit():
            search_id = f"LiPo{battery_id.zfill(3)}"
        else:
            search_id = battery_id
        
        battery_ids = inventory_sheet.col_values(1)
        for idx, bid in enumerate(battery_ids, start=1):
            if bid.strip() == search_id:
                row = inventory_sheet.row_values(idx)
                cells = row[5] if len(row) > 5 else '3S'
                if is_battery_restricted_for_student(cells):
                    return jsonify({
                        'allowed': False,
                        'message': f'Students are not permitted to borrow {cells} batteries. Please ask a staff member for assistance.'
                    })
                else:
                    return jsonify({'allowed': True})
        
        return jsonify({'allowed': True})  # If battery not found, allow anyway
    except Exception as e:
        print(f"Access check error: {e}")
        return jsonify({'allowed': True})  # Allow if error

@app.route('/', methods=['GET', 'POST'])
def index():
    # Check if user is authenticated via session
    if not session.get('authenticated'):
        return render_template('battery_form.html', require_password=True)
    
    if request.method == 'POST':
        battery_id = request.form['battery_id']
        quantity = request.form['quantity']
        name = request.form['name']
        usertype = request.form['usertype']
        action = request.form['action']
        condition = request.form.get('condition', '')
        
        # Normalize battery ID
        if battery_id.isdigit():
            battery_id = f"LiPo{battery_id.zfill(3)}"
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Get battery info
        battery_info = None
        try:
            battery_ids = inventory_sheet.col_values(1)
            for idx, bid in enumerate(battery_ids, start=1):
                if bid.strip() == battery_id:
                    row = inventory_sheet.row_values(idx)
                    battery_info = {
                        'brand': row[1] if len(row) > 1 else '',
                        'type': row[2] if len(row) > 2 else '',
                        'capacity': row[3] if len(row) > 3 else '',
                        'voltage': row[4] if len(row) > 4 else '',
                        'cells': row[5] if len(row) > 5 else '3S'
                    }
                    break
        except Exception as e:
            print(f"Error getting battery info: {e}")
        
        # Get cell voltages (only for Return)
        cell_voltages = []
        min_v = max_v = diff_v = ''
        suggestion = ''
        
        if action == 'Return':
            cell_count = parse_cells(battery_info['cells'] if battery_info else '3S')
            for i in range(1, cell_count + 1):
                cell_key = f'cell{i}'
                val = request.form.get(cell_key, '')
                cell_voltages.append(val if val else '')
            
            # Calculate health stats
            valid_voltages = [float(v) for v in cell_voltages if v and float(v) > 0]
            if valid_voltages:
                min_v = min(valid_voltages)
                max_v = max(valid_voltages)
                diff_v = max_v - min_v
                avg_v = sum(valid_voltages) / len(valid_voltages)
                suggestion = get_battery_suggestion(condition, avg_v, action)
            else:
                min_v = max_v = diff_v = ''
                suggestion = 'No voltage data available.'
        else:
            # Borrow - no cell voltages needed
            suggestion = 'Battery borrowed successfully.'
        
        # Log to Borrow Log sheet
        safe_append_row(sheet, [timestamp, battery_id, name, usertype, action, condition, quantity])
        
        # Log to Cell Voltages sheet
        voltage_row = [timestamp, battery_id, name, usertype, action, quantity]
        for i in range(6):
            voltage_row.append(cell_voltages[i] if i < len(cell_voltages) else '')
        voltage_row.extend([min_v, max_v, diff_v, condition, suggestion])
        safe_append_row(voltage_sheet, voltage_row)
        
        # Update Battery Status
        update_battery_status(battery_id, action, name)
        
        # Return response with suggestion
        return jsonify({
            'success': True,
            'message': 'Submission Recorded Successfully',
            'suggestion': suggestion
        })
    
    return render_template('battery_form.html', require_password=False)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)