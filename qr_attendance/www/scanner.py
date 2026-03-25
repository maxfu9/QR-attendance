import frappe
import os

def get_context(context):
	# Isolated view: bypass all theme/layout
	app_path = frappe.get_app_path('qr_attendance')
	# We'll use a version of scanner.html that is truly standalone
	html_path = os.path.join(app_path, 'www', 'scanner.html')
	if not os.path.exists(html_path):
		html_path = os.path.join(app_path, 'qr_attendance', 'www', 'scanner.html')
	
	if not os.path.exists(html_path):
		return "<h1>Error: scanner.html not found</h1>"
		
	html = frappe.read_file(html_path)
	
	from werkzeug.wrappers import Response
	return Response(html, mimetype='text/html')
