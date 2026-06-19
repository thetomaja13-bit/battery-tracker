from flask import Flask, render_template, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import re
import os
import json

app = Flask(__name__)

# --- Find credentials.json in multiple locations ---
def find_credentials():
    # Possible locations
    possible_paths = [
        "credentials.json",  # Current directory
        "/etc/secrets/credentials.json",  # Render's Secret File location
        os.path.join(os.path.dirname(__file__), "credentials.json"),  # Same folder as app.py
        os.path.join(os.getcwd(), "credentials.json"),  # Working directory
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            print(f"Found credentials at: {path}")
            return path
    
    # If not found, try to read from environment variable
    env_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if env_json:
        print("Using credentials from environment variable")
        # Save it to a temporary file
        temp_path = "/tmp/credentials.json"
        with open(temp_path, "w") as f:
            f.write(env_json)
        return temp_path
    
    raise FileNotFoundError("Could not find credentials.json anywhere")

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Find and load credentials
try:
    creds_path = find_credentials()
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
    print("✅ Credentials loaded successfully!")
except Exception as e:
    print(f"❌ Error loading credentials: {e}")
    raise

client = gspread.authorize(creds)

SHEET_ID = "1U3VIktKv4S0w5SUx6IE2XIhiV63f9YA1s6CxSOBtNeg"

spreadsheet = client.open_by_key(SHEET_ID)

# --- Get or create sheets ---
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
    voltage_sheet.update('A1:N1', [['Timestamp', 'Battery ID', 'Name', 'Action', 
                                    'Cell 1', 'Cell 2', 'Cell 3', 'Cell 4', 'Cell 5', 'Cell 6',
                                    'Min Voltage', 'Max Voltage', 'Difference', 'Condition']])

def parse_cells(cells_str):
    if not cells_str:
        return 3
    match = re.search(r'(\d+)', str(cells_str))
    return int(match.group(1)) if match else 3

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
                print(f"✅ Updated: {battery_id} -> Borrowed by {name}")
            elif action == "Return":
                status_sheet.update_cell(row_number, 2, "Available")
                status_sheet.update_cell(row_number, 3, "")
                print(f"✅ Updated: {battery_id} -> Available")
        else:
            new_row = [battery_id, "Borrowed" if action == "Borrow" else "Available", name if action == "Borrow" else ""]
            status_sheet.append_row(new_row)
            print(f"✅ Added new battery: {battery_id} -> {new_row[1]}")
    except Exception as e:
        print(f"❌ Status update error: {e}")

@app.route('/battery/<battery_id>', methods=['GET', 'POST'])
def battery_form(battery_id):
    battery_info = None
    try:
        battery_ids = inventory_sheet.col_values(1)
        for i, bid in enumerate(battery_ids, start=1):
            if bid.strip() == battery_id:
                row = inventory_sheet.row_values(i)
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

    if request.method == 'POST':
        name = request.form['name']
        usertype = request.form['usertype']
        action = request.form['action']
        condition = request.form['condition']
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cell_count = parse_cells(battery_info['cells'] if battery_info else '3S')
        cell_voltages = []
        for i in range(1, cell_count + 1):
            cell_key = f'cell{i}'
            val = request.form.get(cell_key, '')
            cell_voltages.append(val if val else '')
        
        valid_voltages = [float(v) for v in cell_voltages if v and float(v) > 0]
        if valid_voltages:
            min_v = min(valid_voltages)
            max_v = max(valid_voltages)
            diff_v = max_v - min_v
        else:
            min_v = max_v = diff_v = ''
        
        sheet.append_row([timestamp, battery_id, name, request.form['usertype'], action, condition])
        
        voltage_row = [timestamp, battery_id, name, action]
        for i in range(6):
            voltage_row.append(cell_voltages[i] if i < len(cell_voltages) else '')
        voltage_row.extend([min_v, max_v, diff_v, condition])
        
        try:
            voltage_sheet.append_row(voltage_row)
        except Exception as e:
            print(f"Voltage logging error: {e}")
        
        update_battery_status(battery_id, action, name)
        
        return "Submission Recorded Successfully"
    
    return render_template('battery_form.html', battery_id=battery_id, battery_info=battery_info)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)