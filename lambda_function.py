"""Quote-to-Cash Sync: turns Closed Won HubSpot deals into Zoho Books invoices.

Runs on AWS Lambda, triggered every minute by Amazon EventBridge.
Uses only the Python standard library, so it deploys as a single file.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

HUBSPOT_API = "https://api.hubapi.com"


def http_json(url, method="GET", headers=None, body=None):
    """Make an API call and return the JSON response."""
    if body is not None:
        data = json.dumps(body).encode()
    elif method == "POST":
        data = b""
    else:
        data = None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        # Drop the query string so secrets in it never reach the logs.
        safe_url = url.split("?")[0]
        details = error.read().decode()[:300]
        raise RuntimeError(f"{method} {safe_url} failed: {error.code} {details}") from None


# ---------- HubSpot CRM ----------

def hubspot_headers():
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}"}


def hubspot_get(path):
    return http_json(f"{HUBSPOT_API}{path}", headers=hubspot_headers())


def find_unbilled_closed_won_deals():
    """Deals that are Closed Won but have no Zoho invoice number yet."""
    body = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"},
                {"propertyName": "zoho_invoice_number", "operator": "NOT_HAS_PROPERTY"},
            ]
        }],
        "properties": ["dealname"],
        "limit": 20,
    }
    data = http_json(f"{HUBSPOT_API}/crm/v3/objects/deals/search",
                     method="POST", headers=hubspot_headers(), body=body)
    return [d["id"] for d in data.get("results", [])]


def get_deal_details(deal_id):
    """Collect everything Zoho Books needs from the deal, its company and its contact."""
    deal = hubspot_get(
        f"/crm/v3/objects/deals/{deal_id}"
        "?properties=dealname,amount,dealstage&associations=companies,contacts"
    )
    associations = deal.get("associations", {})
    company_ids = [r["id"] for r in associations.get("companies", {}).get("results", [])]
    contact_ids = [r["id"] for r in associations.get("contacts", {}).get("results", [])]

    if not contact_ids:
        raise RuntimeError(f"Deal {deal_id} has no contact, so there is no email to invoice")
    contact = hubspot_get(f"/crm/v3/objects/contacts/{contact_ids[0]}?properties=email,firstname,lastname")["properties"]

    if company_ids:
        company = hubspot_get(f"/crm/v3/objects/companies/{company_ids[0]}?properties=name")["properties"]
        customer_name = company.get("name")
    else:
        customer_name = f"{contact.get('firstname', '')} {contact.get('lastname', '')}".strip()

    return {
        "deal_id": deal_id,
        "deal_name": deal["properties"].get("dealname"),
        "amount": float(deal["properties"].get("amount") or 0),
        "customer_name": customer_name,
        "email": contact.get("email"),
        "first_name": contact.get("firstname"),
        "last_name": contact.get("lastname"),
    }


def update_hubspot_deal(deal_id, properties):
    """Write values back onto the HubSpot deal."""
    http_json(f"{HUBSPOT_API}/crm/v3/objects/deals/{deal_id}",
              method="PATCH", headers=hubspot_headers(), body={"properties": properties})


# ---------- Zoho Books ----------

def get_zoho_access_token():
    """Swap the long-lived refresh token for a fresh one-hour access token."""
    params = urllib.parse.urlencode({
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    })
    data = http_json(f"{os.environ['ZOHO_ACCOUNTS_URL']}/oauth/v2/token?{params}", method="POST")
    if "access_token" not in data:
        raise RuntimeError(f"Zoho token refresh failed: {data.get('error', data)}")
    return data["access_token"]


def zoho_request(path, token, method="GET", body=None, params=None):
    """Call the Zoho Books API and fail loudly unless Zoho reports code 0."""
    query = {"organization_id": os.environ["ZOHO_ORG_ID"]}
    if params:
        query.update(params)
    url = f"{os.environ['ZOHO_API_URL']}/books/v3{path}?{urllib.parse.urlencode(query)}"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    data = http_json(url, method=method, headers=headers, body=body)
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho Books {method} {path} failed: {data.get('code')} {data.get('message')}")
    return data


def find_or_create_customer(deal, token):
    """Match on email so the same person never becomes two customers."""
    contacts = zoho_request("/contacts", token, params={"email": deal["email"]})["contacts"]
    customers = [c for c in contacts if c.get("contact_type") == "customer"]
    if customers:
        print(f"Found existing Zoho customer: {customers[0]['contact_name']}")
        return customers[0]["contact_id"]

    body = {
        "contact_name": deal["customer_name"],
        "company_name": deal["customer_name"],
        "contact_type": "customer",
        "contact_persons": [{
            "first_name": deal["first_name"],
            "last_name": deal["last_name"],
            "email": deal["email"],
            "is_primary_contact": True,
        }],
    }
    contact = zoho_request("/contacts", token, method="POST", body=body)["contact"]
    print(f"Created Zoho customer: {contact['contact_name']}")
    return contact["contact_id"]


def find_or_create_invoice(deal, customer_id, token):
    """One invoice per deal, even if the same deal is processed twice."""
    reference = f"HS-{deal['deal_id']}"
    existing = zoho_request("/invoices", token, params={"reference_number": reference})["invoices"]
    if existing:
        print(f"Invoice already exists for this deal: {existing[0]['invoice_number']}")
        return existing[0]

    body = {
        "customer_id": customer_id,
        "reference_number": reference,
        "line_items": [{"name": deal["deal_name"], "rate": deal["amount"], "quantity": 1}],
    }
    invoice = zoho_request("/invoices", token, method="POST", body=body)["invoice"]
    print(f"Created invoice {invoice['invoice_number']}")
    return invoice


# ---------- The flow ----------

def process_deal(deal_id):
    deal = get_deal_details(deal_id)
    token = get_zoho_access_token()
    customer_id = find_or_create_customer(deal, token)
    invoice = find_or_create_invoice(deal, customer_id, token)
    update_hubspot_deal(deal_id, {
        "zoho_invoice_number": invoice["invoice_number"],
        "zoho_payment_status": invoice["status"],
    })
    result = {
        "deal_id": deal_id,
        "invoice_id": invoice["invoice_id"],
        "invoice_number": invoice["invoice_number"],
        "status": invoice["status"],
    }
    print(f"Result: {json.dumps(result)}")
    return result


def lambda_handler(event, context):
    # Manual test from the Lambda console: {"deal_id": "..."}
    if "deal_id" in event:
        return {"ok": True, **process_deal(str(event["deal_id"]))}

    # Scheduled run: find Closed Won deals that haven't been invoiced yet.
    deal_ids = find_unbilled_closed_won_deals()
    print(f"Closed Won deals without an invoice: {deal_ids}")
    results, failures = [], []
    for deal_id in deal_ids:
        try:
            results.append(process_deal(deal_id))
        except Exception as error:
            # One bad deal shouldn't block the others; it will be retried next run.
            print(f"Failed deal {deal_id}: {error}")
            failures.append(deal_id)
    return {"ok": not failures, "processed": results, "failed": failures}
