
import frappe
import os

def get_context(context):
	# This bypasses the standard template rendering if we set the response directly
	app_path = frappe.get_app_path('manager_scanner')
	html_path = os.path.join(app_path, 'www', 'scanner.html')
	if not os.path.exists(html_path):
		html_path = os.path.join(app_path, 'manager_scanner', 'www', 'scanner.html')
	
	if os.path.exists(html_path):
		with open(html_path, 'r') as f:
			html = f.read()
		
		# Ensure the token is handled (it's already in the JS of scanner.html via URL params, but we could inject it here too)
		
		frappe.local.response.type = 'text/html'
		frappe.local.response.message = html
	
	return context
