# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
import frappe.defaults
from frappe import _, throw
from frappe.contacts.doctype.address.address import get_address_display
from frappe.contacts.doctype.contact.contact import get_contact_name
from frappe.utils import cint, cstr, flt, get_fullname
from frappe.utils.nestedset import get_root_of

from erpnext.accounts.utils import get_account_name
from webshop.webshop.doctype.webshop_settings.webshop_settings import (
    get_shopping_cart_settings,
)
from webshop.webshop.utils.product import get_web_item_qty_in_stock
from erpnext.selling.doctype.quotation.quotation import _make_sales_order
from erpnext.stock.doctype.packed_item.packed_item import get_product_bundle_items


class WebsitePriceListMissingError(frappe.ValidationError):
    pass


def set_cart_count(quotation=None):
	if cint(frappe.db.get_singles_value("Webshop Settings", "enabled")):
		if not quotation:
			quotation = _get_cart_quotation()
		cart_count = cstr(cint(quotation.get("total_qty")))

		if hasattr(frappe.local, "cookie_manager"):
			frappe.local.cookie_manager.set_cookie("cart_count", cart_count)


@frappe.whitelist()
def get_cart_quotation(doc=None):
	party = get_party()

	if not doc:
		quotation = _get_cart_quotation(party)
		doc = quotation
		set_cart_count(quotation)

	addresses = get_address_docs(party=party)

	if not doc.customer_address and addresses:
		update_cart_address("billing", addresses[0].name)

	return {
		"doc": decorate_quotation_doc(doc),
		"shipping_addresses": get_shipping_addresses(party),
		"billing_addresses": get_billing_addresses(party),
		"shipping_rules": get_applicable_shipping_rules(party),
		"cart_settings": frappe.get_cached_doc("Webshop Settings"),
	}


@frappe.whitelist()
def get_shipping_addresses(party=None):
	if not party:
		party = get_party()
	addresses = get_address_docs(party=party)
	return [
		{
			"name": address.name,
			"title": address.address_title,
			"display": address.display,
		}
		for address in addresses
		if address.address_type == "Shipping"
	]


@frappe.whitelist()
def get_billing_addresses(party=None):
	if not party:
		party = get_party()
	addresses = get_address_docs(party=party)
	return [
		{
			"name": address.name,
			"title": address.address_title,
			"display": address.display,
		}
		for address in addresses
		if address.address_type == "Billing"
	]


@frappe.whitelist()
def place_order():
	quotation = _get_cart_quotation()
	cart_settings = frappe.get_cached_doc("Webshop Settings")
	quotation.company = cart_settings.company

	quotation.flags.ignore_permissions = True
	quotation.submit()

	if quotation.quotation_to == "Lead" and quotation.party_name:
		# company used to create customer accounts
		frappe.defaults.set_user_default("company", quotation.company)

	if not (quotation.shipping_address_name or quotation.customer_address):
		frappe.throw(_("Set Shipping Address or Billing Address"))

	sales_order = frappe.get_doc(
		_make_sales_order(
			quotation.name, ignore_permissions=True
		)
	)
	sales_order.payment_schedule = []

	if not cint(cart_settings.allow_items_not_in_stock):
		for item in sales_order.get("items"):
			item.warehouse = frappe.db.get_value(
				"Website Item", {"item_code": item.item_code}, "website_warehouse"
			)
			is_stock_item = frappe.db.get_value("Item", item.item_code, "is_stock_item")

			if is_stock_item:
				item_stock = get_web_item_qty_in_stock(
					item.item_code, "website_warehouse"
				)
				if not cint(item_stock.in_stock):
					throw(_("{0} Not in Stock").format(item.item_code))
				if item.qty > item_stock.stock_qty:
					throw(
						_("Only {0} in Stock for item {1}").format(
							item_stock.stock_qty, item.item_code
						)
					)

	sales_order.flags.ignore_permissions = True
	sales_order.insert()
	sales_order.submit()

	if hasattr(frappe.local, "cookie_manager"):
		frappe.local.cookie_manager.delete_cookie("cart_count")

	return sales_order.name


@frappe.whitelist()
def _add_package_items_to_quotation(item_code, qty, quotation, warehouse=None):
	"""Add package items from the subitems_list attribute of an item to the quotation
	and set the parent item's rate as the sum of its package items' rates"""
	try:
		# First check if this is a product bundle
		product_bundle_items = get_product_bundle_items(item_code)
		
		# If it's not a product bundle, check if it has subitems_list
		if not product_bundle_items:
			return
		
		package_items = product_bundle_items

		print("package_items=>", package_items)
		
		# Find the parent item in the quotation
		parent_item = None
		for item in quotation.get("items", []):
			if item.item_code == item_code:
				parent_item = item
				break
		
		if not parent_item:
			return
		
		# Track the total price of all package items
		total_package_price = 0
		
		# Add each package item to the quotation
		for package_item in package_items:
			# Get the package item code
			package_item_code = package_item.item_code
			if not package_item_code:
				continue
			
			# Calculate the quantity of the package item based on the quantity of the parent item
			package_item_qty = flt(package_item.qty) * flt(qty)
			
			# Get the warehouse for the package item
			package_item_warehouse = frappe.get_cached_value(
				"Website Item", {"item_code": package_item_code}, "website_warehouse"
			) or warehouse
			
			# Get the price of the package item
			package_item_price = 0
			if hasattr(package_item, 'rate') and package_item.rate:
				package_item_price = flt(package_item.rate)
			else:
				# Try to get the price from Item Price
				item_price = frappe.get_all(
					"Item Price",
					fields=["price_list_rate"],
					filters={
						"item_code": package_item_code,
						"price_list": quotation.selling_price_list,
						"selling": 1
					},
					order_by="valid_from desc",
					limit=1
				)
				
				if item_price:
					package_item_price = flt(item_price[0].price_list_rate)
				else:
					# If no price found in the price list, try to get the standard rate from Item
					standard_rate = frappe.db.get_value("Item", package_item_code, "standard_rate")
					if standard_rate:
						package_item_price = flt(standard_rate)
			
			# Add to the total price
			total_package_price += package_item_price * flt(package_item.qty)
			
			# Check if the package item has subitems and add them to the quotation
			_add_subitems_to_quotation(quotation, package_item_code, package_item_qty, package_item_warehouse)
				
		# Set the parent item's rate to the sum of its package items' rates
		parent_item.rate = total_package_price
		parent_item.price_list_rate = total_package_price
		parent_item.amount = total_package_price * flt(qty)
		
	except Exception as e:
		frappe.log_error(f"Error adding package items to quotation: {str(e)}")
		print(f"Error adding subitems to quotation: {str(e)}")

def _add_subitems_to_quotation(quotation, item_code, qty, warehouse):
	# Get the item document to access its subitems_list
	item_doc = frappe.get_doc("Item", item_code)
	
	# Check if the item has subitems_list
	if not hasattr(item_doc, 'subitems_list') or not item_doc.subitems_list:
		return
			
	subitems = item_doc.subitems_list

	for subitem in subitems:
		quotation_items = quotation.get("items", {"item_code": subitem.item_code})
		if not quotation_items:	
			print("subitem===>", subitem.as_dict())
			# Add the subitem to the quotation
			quotation.append(
				"items",
				{
					"doctype": "Quotation Item",
					"item_code": subitem.item_code,
					"qty": subitem.qty,
					"warehouse": warehouse,
					"parent_item": item_code
				},
			)
		else:
			# Update the quantity of the existing subitem
			total_qty = 0
			
			# Search for all parent items in the quotation that have this subitem in their subitems_list
			for parent_item in quotation.items:
				# Skip the subitem itself
				if parent_item.item_code == subitem.item_code:
					continue
					
				# Get the parent item document
				try:
					parent_doc = frappe.get_doc("Item", parent_item.item_code)
					
					# Check if the parent item has subitems_list
					if hasattr(parent_doc, 'subitems_list') and parent_doc.subitems_list:
						# Check if the current subitem is in the parent's subitems_list
						for parent_subitem in parent_doc.subitems_list:
							if parent_subitem.item_code == subitem.item_code:
								# Add the quantity based on the parent item's quantity and subitem's quantity
								total_qty += flt(parent_item.qty) * flt(parent_subitem.qty)
								break
				except Exception as e:
					frappe.log_error(f"Error calculating subitem quantity: {str(e)}")
			
			# Set the calculated total quantity
			if total_qty > 0:
				quotation_items[0].qty = total_qty
			else:
				# If no parent items found, set the default quantity
				quotation_items[0].qty = subitem.qty * qty

@frappe.whitelist()
def request_for_quotation():
	# First, get the cart quotation
	quotation = _get_cart_quotation()
	quotation.flags.ignore_permissions = True
	
	# Check if shipping address is set
	if not quotation.shipping_address_name:
		frappe.throw(_("Please set a shipping address before requesting a quotation."))
	
	# Check if the quotation is already linked to a project
	if quotation.project_name:
		existing_project = frappe.db.exists("Project", quotation.project_name)
		if existing_project:
			# Return the existing project name if it already exists
			return quotation.project_name
	
	# Get customer details from quotation
	customer_name = frappe.db.get_value("Customer", quotation.party_name, "customer_name")
	
	# Generate a project name based on the quotation
	project_name = f"RFQ-{frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S')}"
	
	# Create a new project with part_status "New request" using get_doc
	project = frappe.get_doc({
		"doctype": "Project",
		"project_name": project_name,
		"status": "",
		"parts_status": "New request",
		"expected_start_date": frappe.utils.today(),
		"customer": quotation.party_name,
		"customer_name": customer_name,
		"priority": "Medium",
		"is_active": "Yes",
		"plate": project_name,
		"queue_position": 1,
	})
	
	# Save the project
	project.flags.ignore_permissions = True
	project.insert()
	
	# Link the quotation to the project
	quotation.project_name = project.name
	quotation.save()
	
	return project.name


@frappe.whitelist()
def update_cart(item_code, qty, additional_notes=None, with_items=False):
	quotation = _get_cart_quotation()

	empty_card = False
	qty = flt(qty)
	if qty == 0:
		# First, remove any subitems that have this item as their parent_item
		subitems_to_keep = []
		for item in quotation.get("items", []):
			if not hasattr(item, 'parent_item') or item.parent_item != item_code:
				subitems_to_keep.append(item)
		
		# Then remove the parent item
		quotation_items = [item for item in subitems_to_keep if item.item_code != item_code]
		
		if quotation_items:
			quotation.set("items", quotation_items)
		else:
			empty_card = True

	else:
		warehouse = frappe.get_cached_value(
			"Website Item", {"item_code": item_code}, "website_warehouse"
		)

		quotation_items = quotation.get("items", {"item_code": item_code})
		if not quotation_items:
			quotation.append(
				"items",
				{
					"doctype": "Quotation Item",
					"item_code": item_code,
					"qty": qty,
					"additional_notes": additional_notes,
					"warehouse": warehouse,
				},
			)
		else:
			quotation_items[0].qty = qty
			quotation_items[0].warehouse = warehouse
			quotation_items[0].additional_notes = additional_notes
	
	apply_cart_settings(quotation=quotation)

	# Only add package items when adding to cart (qty > 0), not when removing
	if qty > 0:
		# Add package items to the quotation and update the parent item's rate
		_add_package_items_to_quotation(item_code, qty, quotation, warehouse)
		# Add subitems to the quotation and update the parent item's rate
		_add_subitems_to_quotation(quotation, item_code, qty, warehouse)

	quotation.flags.ignore_permissions = True
	quotation.payment_schedule = []
	if not empty_card:
		quotation.save()
	else:
		quotation.delete()
		quotation = None

	set_cart_count(quotation)

	if cint(with_items):
		context = get_cart_quotation(quotation)
		return {
			"items": frappe.render_template(
				"templates/includes/cart/cart_items.html", context
			),
			"total": frappe.render_template(
				"templates/includes/cart/cart_items_total.html", context
			),
			"taxes_and_totals": frappe.render_template(
				"templates/includes/cart/cart_payment_summary.html", context
			),
		}
	else:
		return {"name": quotation.name}


@frappe.whitelist()
def get_shopping_cart_menu(context=None):
	if not context:
		context = get_cart_quotation()

	return frappe.render_template("templates/includes/cart/cart_dropdown.html", context)


@frappe.whitelist()
def add_new_address(doc):
	doc = frappe.parse_json(doc)
	doc.update({"doctype": "Address"})
	
	# Remove '+' prefix from phone number if present
	if doc.get('phone') and doc['phone'].startswith('+'):
		doc['phone'] = doc['phone'].lstrip('+')
	
	address = frappe.get_doc(doc)
	address.save(ignore_permissions=True)

	# Update customer's phone_number and contact's phone if provided in the address
	if address.phone:
		party = get_party()
		
		if party and party.doctype == "Customer":
			# Update customer's phone_number
			# Ensure phone number doesn't have '+' prefix
			phone_number = address.phone
			if phone_number.startswith('+'):
				phone_number = phone_number.lstrip('+')
			
			party.phone_number = phone_number
			party.flags.ignore_permissions = True
			party.save()
			
			# Update contact's phone
			contact_name = frappe.db.get_value("Contact", {"email_id": frappe.session.user})
			
			if contact_name:
				contact = frappe.get_doc("Contact", contact_name)
				contact.phone = phone_number
				contact.flags.ignore_permissions = True
				contact.save()

	return address


@frappe.whitelist(allow_guest=True)
def create_lead_for_item_inquiry(lead, subject, message):
	lead = frappe.parse_json(lead)
	lead_doc = frappe.new_doc("Lead")
	for fieldname in ("lead_name", "company_name", "email_id", "phone"):
		lead_doc.set(fieldname, lead.get(fieldname))

	lead_doc.set("lead_owner", "")

	if not frappe.db.exists("Lead Source", "Product Inquiry"):
		frappe.get_doc(
			{"doctype": "Lead Source", "source_name": "Product Inquiry"}
		).insert(ignore_permissions=True)

	lead_doc.set("source", "Product Inquiry")

	try:
		lead_doc.save(ignore_permissions=True)
	except frappe.exceptions.DuplicateEntryError:
		frappe.clear_messages()
		lead_doc = frappe.get_doc("Lead", {"email_id": lead["email_id"]})

	lead_doc.add_comment(
		"Comment",
		text="""
		<div>
			<h5>{subject}</h5>
			<p>{message}</p>
		</div>
	""".format(
			subject=subject, message=message
		),
	)

	return lead_doc


@frappe.whitelist()
def get_terms_and_conditions(terms_name):
	return frappe.db.get_value("Terms and Conditions", terms_name, "terms")


@frappe.whitelist()
def update_cart_address(address_type, address_name):
	quotation = _get_cart_quotation()
	address_doc = frappe.get_doc("Address", address_name).as_dict()
	address_display = get_address_display(address_doc)

	if address_type.lower() == "billing":
		quotation.customer_address = address_name
		quotation.address_display = address_display
		quotation.shipping_address_name = (
			quotation.shipping_address_name or address_name
		)
		address_doc = next(
			(doc for doc in get_billing_addresses() if doc["name"] == address_name),
			None,
		)
	elif address_type.lower() == "shipping":
		quotation.shipping_address_name = address_name
		quotation.shipping_address = address_display
		quotation.customer_address = quotation.customer_address or address_name
		address_doc = next(
			(doc for doc in get_shipping_addresses() if doc["name"] == address_name),
			None,
		)
	apply_cart_settings(quotation=quotation)

	quotation.flags.ignore_permissions = True
	quotation.save()

	context = get_cart_quotation(quotation)
	context["address"] = address_doc

	return {
		"taxes": frappe.render_template(
			"templates/includes/order/order_taxes.html", context
		),
		"address": frappe.render_template(
			"templates/includes/cart/address_card.html", context
		),
	}


def guess_territory():
	territory = None
	geoip_country = frappe.session.get("session_country")
	if geoip_country:
		territory = frappe.db.get_value("Territory", geoip_country)

	return (
		territory
		or get_root_of("Territory")
	)


def decorate_quotation_doc(doc):
	if not doc:
		return doc
		
	for d in doc.get("items", []):
		item_code = d.item_code
		fields = ["web_item_name", "thumbnail", "website_image", "description", "route"]

		# Variant Item
		if not frappe.db.exists("Website Item", {"item_code": item_code}):
			variant_data = frappe.db.get_values(
				"Item",
				filters={"item_code": item_code},
				fieldname=["variant_of", "item_name", "image"],
				as_dict=True,
			)
			
			if not variant_data:
				continue
				
			variant_data = variant_data[0]
			item_code = variant_data.variant_of
			fields = fields[1:]
			d.web_item_name = variant_data.item_name

			if variant_data.image:  # get image from variant or template web item
				d.thumbnail = variant_data.image
				fields = fields[2:]

		website_item_data = frappe.db.get_value(
			"Website Item", {"item_code": item_code}, fields, as_dict=True
		)
		
		if website_item_data:
			d.update(website_item_data)

		website_warehouse = frappe.get_cached_value(
			"Website Item", {"item_code": item_code}, "website_warehouse"
		)

		d.warehouse = website_warehouse

	return doc


def _get_cart_quotation(party=None):
	"""Return the open Quotation of type "Shopping Cart" or make a new one"""
	if not party:
		party = get_party()

	quotation = frappe.get_all(
		"Quotation",
		fields=["name"],
		filters={
			"party_name": party.name,
			"contact_email": frappe.session.user,
			"order_type": "Shopping Cart",
			"docstatus": 0,
		},
		order_by="modified desc",
		limit_page_length=1,
	)

	if quotation:
		qdoc = frappe.get_doc("Quotation", quotation[0].name)
	else:
		company = frappe.db.get_single_value("Webshop Settings", "company")
		qdoc = frappe.get_doc(
			{
				"doctype": "Quotation",
				"quotation_to": party.doctype,
				"company": company,
				"order_type": "Shopping Cart",
				"status": "Draft",
				"docstatus": 0,
				"__islocal": 1,
				"party_name": party.name,
			}
		)

		qdoc.contact_person = frappe.db.get_value(
			"Contact", {"email_id": frappe.session.user}
		)
		qdoc.contact_email = frappe.session.user

		qdoc.flags.ignore_permissions = True
		qdoc.run_method("set_missing_values")
		apply_cart_settings(party, qdoc)

	return qdoc


def update_party(fullname, company_name=None, mobile_no=None, phone=None):
	party = get_party()

	party.customer_name = company_name or fullname
	party.customer_type = "Company" if company_name else "Individual"

	contact_name = frappe.db.get_value("Contact", {"email_id": frappe.session.user})
	contact = frappe.get_doc("Contact", contact_name)
	contact.first_name = fullname
	contact.last_name = None
	contact.customer_name = party.customer_name
	contact.mobile_no = mobile_no
	contact.phone = phone
	contact.flags.ignore_permissions = True
	contact.save()

	party_doc = frappe.get_doc(party.as_dict())
	party_doc.flags.ignore_permissions = True
	party_doc.save()

	qdoc = _get_cart_quotation(party)
	if not qdoc.get("__islocal"):
		qdoc.customer_name = company_name or fullname
		qdoc.run_method("set_missing_lead_customer_details")
		qdoc.flags.ignore_permissions = True
		qdoc.save()


def apply_cart_settings(party=None, quotation=None):
	if not party:
		party = get_party()
	if not quotation:
		quotation = _get_cart_quotation(party)

	cart_settings = frappe.get_cached_doc("Webshop Settings")

	set_price_list_and_rate(quotation, cart_settings)

	quotation.run_method("calculate_taxes_and_totals")

	set_taxes(quotation, cart_settings)

	_apply_shipping_rule(party, quotation, cart_settings)


def set_price_list_and_rate(quotation, cart_settings):
	"""set price list based on billing territory"""

	_set_price_list(cart_settings, quotation)

	# reset values
	quotation.price_list_currency = (
		quotation.currency
	) = quotation.plc_conversion_rate = quotation.conversion_rate = None
	for item in quotation.get("items"):
		item.price_list_rate = item.discount_percentage = item.rate = item.amount = None

	# refetch values
	quotation.run_method("set_price_list_and_item_details")

	if hasattr(frappe.local, "cookie_manager"):
		# set it in cookies for using in product page
		frappe.local.cookie_manager.set_cookie(
			"selling_price_list", quotation.selling_price_list
		)


def _set_price_list(cart_settings, quotation=None):
	"""Set price list based on customer or shopping cart default"""
	from erpnext.accounts.party import get_default_price_list

	party_name = quotation.get("party_name") if quotation else get_party().get("name")
	selling_price_list = None

	# check if default customer price list exists
	if party_name and frappe.db.exists("Customer", party_name):
		selling_price_list = get_default_price_list(
			frappe.get_doc("Customer", party_name)
		)

	# check default price list in shopping cart
	if not selling_price_list:
		selling_price_list = cart_settings.price_list

	if quotation:
		quotation.selling_price_list = selling_price_list

	return selling_price_list


def set_taxes(quotation, cart_settings):
	"""set taxes based on billing territory"""
	from erpnext.accounts.party import set_taxes

	customer_group = frappe.db.get_value(
		"Customer", quotation.party_name, "customer_group"
	)

	quotation.taxes_and_charges = set_taxes(
		quotation.party_name,
		"Customer",
		quotation.transaction_date,
		quotation.company,
		customer_group=customer_group,
		supplier_group=None,
		tax_category=quotation.tax_category,
		billing_address=quotation.customer_address,
		shipping_address=quotation.shipping_address_name,
		use_for_shopping_cart=1,
	)
	#
	# 	# clear table
	quotation.set("taxes", [])
	#
	# 	# append taxes
	quotation.append_taxes_from_master()
	quotation.append_taxes_from_item_tax_template()


def get_party(user=None):
	if not user:
		user = frappe.session.user

	contact_name = get_contact_name(user)
	party = None

	if contact_name:
		contact = frappe.get_doc("Contact", contact_name)
		if contact.links:
			party_doctype = contact.links[0].link_doctype
			party = contact.links[0].link_name

	cart_settings = frappe.get_cached_doc("Webshop Settings")

	debtors_account = ""

	if cart_settings.enable_checkout:
		debtors_account = get_debtors_account(cart_settings)

	if party:
		doc = frappe.get_doc(party_doctype, party)
		if doc.doctype in ["Customer", "Supplier"]:
			if not frappe.db.exists("Portal User", {"parent": doc.name, "user": user}):
				doc.append("portal_users", {"user": user})
				doc.flags.ignore_permissions = True
				doc.flags.ignore_mandatory = True
				doc.save()

		return doc

	elif not frappe.db.exists("Portal User", {"user": user}):
		if not cart_settings.enabled:
			frappe.local.flags.redirect_location = "/contact"
			raise frappe.Redirect
		customer = frappe.new_doc("Customer")
		fullname = get_fullname(user)
		customer.update(
			{
				"customer_name": fullname,
				"customer_type": "Individual",
				"customer_group": get_shopping_cart_settings().default_customer_group,
				"territory": get_root_of("Territory"),
			}
		)

		customer.append("portal_users", {"user": user})

		if debtors_account:
			customer.update(
				{
					"accounts": [
						{"company": cart_settings.company, "account": debtors_account}
					]
				}
			)

		customer.flags.ignore_mandatory = True
		customer.insert(ignore_permissions=True)

		# Check if a contact with this email already exists
		existing_contact = frappe.db.get_value(
			"Contact Email", {"email_id": user}, "parent"
		)
		
		if existing_contact:
			# If contact exists, just link it to the customer
			contact = frappe.get_doc("Contact", existing_contact)
			# Check if this contact is already linked to our customer
			if not any(link.link_name == customer.name for link in contact.links):
				contact.append("links", dict(link_doctype="Customer", link_name=customer.name))
				contact.flags.ignore_mandatory = True
				contact.save(ignore_permissions=True)
		else:
			# Create new contact only if one doesn't exist
			contact = frappe.new_doc("Contact")
			contact.update(
				{"first_name": fullname, "email_ids": [{"email_id": user, "is_primary": 1}]}
			)
			contact.append("links", dict(link_doctype="Customer", link_name=customer.name))
			contact.flags.ignore_mandatory = True
			contact.insert(ignore_permissions=True)

		return customer
	else:
		customer = frappe.db.get_value(
			"Portal User", {"user": user}, ["parent"]
		)

		if frappe.db.exists("Customer", customer):
			return frappe.get_doc("Customer", customer)


def get_debtors_account(cart_settings):
	if not cart_settings.payment_gateway_account:
		frappe.throw(_("Payment Gateway Account not set"), _("Mandatory"))

	payment_gateway_account_currency = frappe.get_doc(
		"Payment Gateway Account", cart_settings.payment_gateway_account
	).currency

	account_name = _("Debtors ({0})").format(payment_gateway_account_currency)

	debtors_account_name = get_account_name(
		"Receivable",
		"Asset",
		is_group=0,
		account_currency=payment_gateway_account_currency,
		company=cart_settings.company,
	)

	if not debtors_account_name:
		debtors_account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_type": "Receivable",
				"root_type": "Asset",
				"is_group": 0,
				"parent_account": get_account_name(
					root_type="Asset", is_group=1, company=cart_settings.company
				),
				"account_name": account_name,
				"currency": payment_gateway_account_currency,
			}
		).insert(ignore_permissions=True)

		return debtors_account.name

	else:
		return debtors_account_name


def get_address_docs(
    doctype=None,
    txt=None,
    filters=None,
    limit_start=0,
    limit_page_length=20,
    party=None,
):
	if not party:
		party = get_party()

	if not party:
		return []

	address_names = frappe.db.get_all(
		"Dynamic Link",
		fields=("parent"),
		filters=dict(
			parenttype="Address", link_doctype=party.doctype, link_name=party.name
		),
	)

	out = []

	for a in address_names:
		address = frappe.get_doc("Address", a.parent)
		address.display = get_address_display(address.as_dict())
		out.append(address)

	return out


@frappe.whitelist()
def apply_shipping_rule(shipping_rule):
	quotation = _get_cart_quotation()

	quotation.shipping_rule = shipping_rule

	apply_cart_settings(quotation=quotation)

	quotation.flags.ignore_permissions = True
	quotation.save()

	return get_cart_quotation(quotation)


def _apply_shipping_rule(party=None, quotation=None, cart_settings=None):
	if not quotation.shipping_rule:
		shipping_rules = get_shipping_rules(quotation, cart_settings)

		if not shipping_rules:
			return

		elif quotation.shipping_rule not in shipping_rules:
			quotation.shipping_rule = shipping_rules[0]

	if quotation.shipping_rule:
		quotation.run_method("apply_shipping_rule")
		quotation.run_method("calculate_taxes_and_totals")


def get_applicable_shipping_rules(party=None, quotation=None):
	shipping_rules = get_shipping_rules(quotation)

	if shipping_rules:
		rule_label_map = frappe.db.get_values("Shipping Rule", shipping_rules, "label")
		# we need this in sorted order as per the position of the rule in the settings page
		return [[rule, rule] for rule in shipping_rules]


def get_shipping_rules(quotation=None, cart_settings=None):
	if not quotation:
		quotation = _get_cart_quotation()

	shipping_rules = []
	if quotation.shipping_address_name:
		country = frappe.db.get_value(
			"Address", quotation.shipping_address_name, "country"
		)
		if country:
			sr_country = frappe.qb.DocType("Shipping Rule Country")
			sr = frappe.qb.DocType("Shipping Rule")
			query = (
				frappe.qb.from_(sr_country)
				.join(sr)
				.on(sr.name == sr_country.parent)
				.select(sr.name)
				.distinct()
				.where((sr_country.country == country) & (sr.disabled != 1))
			)
			result = query.run(as_list=True)
			shipping_rules = [x[0] for x in result]

	return shipping_rules


def get_address_territory(address_name):
	"""Tries to match city, state and country of address to existing territory"""
	territory = None

	if address_name:
		address_fields = frappe.db.get_value(
			"Address", address_name, ["city", "state", "country"]
		)
		for value in address_fields:
			territory = frappe.db.get_value("Territory", value)
			if territory:
				break

	return territory


def show_terms(doc):
	return doc.tc_name


@frappe.whitelist(allow_guest=True)
def apply_coupon_code(applied_code, applied_referral_sales_partner):
	quotation = True

	if not applied_code:
		frappe.throw(_("Please enter a coupon code"))

	coupon_list = frappe.get_all("Coupon Code", filters={"coupon_code": applied_code})
	if not coupon_list:
		frappe.throw(_("Please enter a valid coupon code"))

	coupon_name = coupon_list[0].name

	from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code

	validate_coupon_code(coupon_name)
	quotation = _get_cart_quotation()
	quotation.coupon_code = coupon_name
	quotation.flags.ignore_permissions = True
	quotation.save()

	if applied_referral_sales_partner:
		sales_partner_list = frappe.get_all(
			"Sales Partner", filters={"referral_code": applied_referral_sales_partner}
		)
		if sales_partner_list:
			sales_partner_name = sales_partner_list[0].name
			quotation.referral_sales_partner = sales_partner_name
			quotation.flags.ignore_permissions = True
			quotation.save()

	return quotation
