import frappe
import base64
import hashlib
import hmac
import json
import math
import os
from frappe import _
from frappe.utils import cint, flt, now_datetime, time_diff_in_seconds
from hrms.hr.doctype.employee_checkin.employee_checkin import validate_active_employee

DEFAULT_SCAN_COOLDOWN_SECONDS = 60
DEFAULT_ALLOWED_RADIUS_METERS = 100
SIGNED_QR_PREFIX = "msqr1"


def get_manager_scanner_settings():
	return frappe.get_cached_doc("Manager Scanner Settings")


def validate_token(token):
	if not token:
		frappe.throw(_("Missing token"), frappe.PermissionError)

	manager_token = frappe.db.get_single_value("Manager Scanner Settings", "manager_token")
	if not manager_token or token != manager_token:
		frappe.throw(_("Invalid token"), frappe.PermissionError)


def get_scan_cooldown_seconds():
	settings = get_manager_scanner_settings()
	return int(getattr(settings, "scan_cooldown_seconds", 0) or DEFAULT_SCAN_COOLDOWN_SECONDS)


def should_enforce_signed_qr_codes():
	settings = get_manager_scanner_settings()
	return cint(getattr(settings, "enforce_signed_qr_codes", 0))


def is_location_validation_enabled():
	settings = get_manager_scanner_settings()
	return cint(getattr(settings, "enable_location_validation", 0))


def get_qr_signing_secret():
	return frappe.local.conf.get("encryption_key") or frappe.local.conf.get("db_name") or "manager_scanner"


def _urlsafe_b64encode(value):
	return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _urlsafe_b64decode(value):
	padding = "=" * (-len(value) % 4)
	return base64.urlsafe_b64decode((value + padding).encode())


def get_signed_qr_payload(employee_id):
	payload = {"employee": employee_id}
	payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
	payload_b64 = _urlsafe_b64encode(payload_json)
	signature = hmac.new(
		get_qr_signing_secret().encode(),
		payload_b64.encode(),
		hashlib.sha256,
	).hexdigest()
	return f"{SIGNED_QR_PREFIX}.{payload_b64}.{signature}"


def resolve_employee_id_from_scan(scan_data):
	if not scan_data:
		frappe.throw(_("Missing scan data"))

	scan_data = scan_data.strip()
	if scan_data.startswith(f"{SIGNED_QR_PREFIX}."):
		try:
			_prefix, payload_b64, signature = scan_data.split(".", 2)
		except ValueError:
			frappe.throw(_("Invalid signed QR format"))

		expected_signature = hmac.new(
			get_qr_signing_secret().encode(),
			payload_b64.encode(),
			hashlib.sha256,
		).hexdigest()
		if not hmac.compare_digest(signature, expected_signature):
			frappe.throw(_("Invalid or tampered QR code"))

		try:
			payload = json.loads(_urlsafe_b64decode(payload_b64).decode())
		except Exception:
			frappe.throw(_("Invalid QR payload"))

		employee_id = payload.get("employee")
		if not employee_id:
			frappe.throw(_("Employee information missing in QR code"))
		return employee_id

	if should_enforce_signed_qr_codes():
		frappe.throw(_("Only signed QR codes are allowed"))

	return scan_data


def get_distance_in_meters(latitude_1, longitude_1, latitude_2, longitude_2):
	earth_radius = 6371000
	lat1 = math.radians(latitude_1)
	lat2 = math.radians(latitude_2)
	lat_delta = math.radians(latitude_2 - latitude_1)
	lon_delta = math.radians(longitude_2 - longitude_1)

	a = (
		math.sin(lat_delta / 2) ** 2
		+ math.cos(lat1) * math.cos(lat2) * math.sin(lon_delta / 2) ** 2
	)
	c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
	return earth_radius * c


def validate_scan_location(latitude=None, longitude=None):
	if not is_location_validation_enabled():
		return

	settings = get_manager_scanner_settings()
	raw_allowed_latitude = getattr(settings, "allowed_latitude", None)
	raw_allowed_longitude = getattr(settings, "allowed_longitude", None)
	allowed_radius_meters = cint(
		getattr(settings, "allowed_radius_meters", 0) or DEFAULT_ALLOWED_RADIUS_METERS
	)

	if raw_allowed_latitude in (None, "") or raw_allowed_longitude in (None, ""):
		frappe.throw(_("Scanner location validation is enabled but coordinates are not configured"))

	if latitude is None or longitude is None:
		frappe.throw(_("Location is required for this scanner"))

	allowed_latitude = flt(raw_allowed_latitude)
	allowed_longitude = flt(raw_allowed_longitude)
	latitude = flt(latitude)
	longitude = flt(longitude)
	distance = get_distance_in_meters(allowed_latitude, allowed_longitude, latitude, longitude)
	if distance > allowed_radius_meters:
		frappe.throw(
			_("You are {0}m away from the allowed scan location. Maximum allowed distance is {1}m.").format(
				int(distance), allowed_radius_meters
			)
		)


def get_next_log_type(employee):
	last_log_type = frappe.db.get_value(
		"Employee Checkin",
		{"employee": employee},
		"log_type",
		order_by="time desc, creation desc",
	)
	return "OUT" if last_log_type == "IN" else "IN"


def validate_scan_cooldown(employee):
	scan_cooldown_seconds = get_scan_cooldown_seconds()
	last_checkin = frappe.db.get_value(
		"Employee Checkin",
		{"employee": employee},
		["time", "log_type"],
		as_dict=True,
		order_by="time desc, creation desc",
	)
	if not last_checkin or not last_checkin.time:
		return

	elapsed_seconds = time_diff_in_seconds(now_datetime(), last_checkin.time)
	if elapsed_seconds >= scan_cooldown_seconds:
		return

	wait_seconds = int(scan_cooldown_seconds - max(elapsed_seconds, 0))
	frappe.throw(
		_("{0} was already scanned as {1}. Please wait {2} seconds before scanning again.").format(
			employee, last_checkin.log_type or _("Unknown"), wait_seconds
		)
	)


def get_employee_scan_context(scan_data):
	employee_id = resolve_employee_id_from_scan(scan_data)
	employee = frappe.db.get_value(
		"Employee",
		{"name": employee_id},
		["name", "employee_name", "department", "designation", "image"],
		as_dict=True,
	)
	if not employee:
		frappe.throw(_("Employee {0} not found").format(employee_id))

	validate_active_employee(employee.name)
	next_log_type = get_next_log_type(employee.name)
	return employee, next_log_type


def get_employee_image_url(image_path, token):
	if not image_path:
		return None
	if image_path.startswith("/files/") or image_path.startswith("http://") or image_path.startswith("https://"):
		return image_path
	if image_path.startswith("/private/files/"):
		return f"/api/method/manager_scanner.api.get_employee_image?token={token}&file_path={image_path}"
	return image_path


@frappe.whitelist(allow_guest=True)
def get_scanner_config(token=None):
	validate_token(token)
	return {
		"scan_cooldown_seconds": get_scan_cooldown_seconds(),
		"location_validation_enabled": bool(is_location_validation_enabled()),
	}


@frappe.whitelist(allow_guest=True)
def get_employee_preview(scan_data=None, employee_id=None, token=None):
	validate_token(token)
	scan_data = scan_data or employee_id
	if not scan_data:
		frappe.throw(_("Employee ID is required"))

	employee, next_log_type = get_employee_scan_context(scan_data)
	return {
		"employee_id": employee.name,
		"employee_name": employee.employee_name,
		"department": employee.department,
		"designation": employee.designation,
		"image": get_employee_image_url(employee.image, token),
		"next_log_type": next_log_type,
	}


@frappe.whitelist(allow_guest=True)
def get_employee_image(file_path=None, token=None):
	validate_token(token)
	if not file_path or not file_path.startswith("/private/files/"):
		frappe.throw(_("Invalid image path"))

	absolute_path = frappe.get_site_path(file_path.lstrip("/"))
	if not os.path.exists(absolute_path):
		frappe.throw(_("Employee image not found"))

	with open(absolute_path, "rb") as image_file:
		content = image_file.read()

	frappe.local.response.filename = os.path.basename(file_path)
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"


@frappe.whitelist(allow_guest=True)
def get_recent_scans(token=None, limit=5):
	validate_token(token)
	limit = max(1, min(int(limit or 5), 20))

	return frappe.get_all(
		"Employee Checkin",
		fields=["employee", "employee_name", "log_type", "time"],
		order_by="time desc, creation desc",
		limit_page_length=limit,
	)


@frappe.whitelist(allow_guest=True)
def mark_attendance(scan_data=None, employee_id=None, log_type=None, token=None, latitude=None, longitude=None):
	"""
	Mark employee attendance via QR scan.
	Guest accessible if the token matches the Manager Scanner Token.
	"""
	validate_token(token)

	scan_data = scan_data or employee_id
	if not scan_data:
		frappe.throw(_("Employee ID is required"))

	try:
		employee, resolved_log_type = get_employee_scan_context(scan_data)
		validate_scan_location(latitude=latitude, longitude=longitude)
		validate_scan_cooldown(employee.name)

		doc = frappe.new_doc("Employee Checkin")
		doc.employee = employee.name
		doc.employee_name = employee.employee_name
		doc.time = now_datetime()
		doc.log_type = resolved_log_type
		doc.latitude = flt(latitude) if latitude is not None else None
		doc.longitude = flt(longitude) if longitude is not None else None
		doc.insert(ignore_permissions=True)

		return {
			"status": "success",
			"log_type": resolved_log_type,
			"message": _("Attendance marked as {0} for {1}").format(
				resolved_log_type, doc.employee_name or doc.employee
			),
		}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), _("Manager Scanner Error"))
		return {
			"status": "error",
			"message": str(e)
		}

@frappe.whitelist(allow_guest=True)
def get_scanner_page(token=None):
	"""
	Returns the raw scanner HTML.
	Bypasses all layout/theme engine.
	"""
	try:
		validate_token(token)
	except Exception:
		return "<h1>Invalid token</h1>"
		
	app_path = frappe.get_app_path('manager_scanner')
	# We'll use a version of scanner.html that is truly standalone
	html_path = os.path.join(app_path, 'www', 'scanner.html')
	if not os.path.exists(html_path):
		html_path = os.path.join(app_path, 'manager_scanner', 'www', 'scanner.html')
	
	if not os.path.exists(html_path):
		return f"<h1>Error: scanner.html not found at {html_path}</h1>"
		
	html = frappe.read_file(html_path)
	
	from werkzeug.wrappers import Response
	return Response(html, mimetype='text/html')

@frappe.whitelist()
def create_fallback_web_page():
	"""
	Create a Web Page that redirects to our raw API view.
	Also ensures default settings are initialized.
	"""
	# Initialize Settings
	if not frappe.db.exists("Manager Scanner Settings"):
		doc = frappe.get_doc({
			"doctype": "Manager Scanner Settings",
			"manager_token": "manager123",
			"scan_cooldown_seconds": 60,
			"allowed_radius_meters": 100
		})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

	name = frappe.db.get_value('Web Page', {'route': 'scanner'}, 'name')
	if name:
		doc = frappe.get_doc('Web Page', name)
	else:
		doc = frappe.new_doc('Web Page')
		doc.title = 'Manager Scanner Redirect'
		doc.route = 'scanner'
		
	doc.published = 1
	doc.full_width = 1
	doc.show_title = 0
	# Redirect via JS
	redirect_html = """
	<script>
		const urlParams = new URLSearchParams(window.location.search);
		const token = urlParams.get('token') || 'manager123';
		window.location.href = '/api/method/manager_scanner.api.get_scanner_page?token=' + token;
	</script>
	<p>Redirecting to scanner...</p>
	"""
	# Frappe stores HTML pages in `main_section_html` when `content_type` is `HTML`.
	doc.main_section = redirect_html
	doc.main_section_html = redirect_html
	doc.content_type = 'HTML'
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	
	return f"Web Page '{doc.name}' and Settings initialized."
