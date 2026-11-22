# Discovery: HubSpot to Zoho Books Quote-to-Cash

This document captures the requirements and decisions behind the integration, written the way a discovery document for a client implementation would be.

## Scenario

A small business runs sales in **HubSpot CRM** and accounting in **Zoho Books**. When a deal is won, a salesperson tells the finance team, who then create the customer and invoice by hand in Zoho Books.

## Current process and pain points

| Step | Today | Problem |
|---|---|---|
| Deal marked Closed Won | Salesperson updates HubSpot | None |
| Finance notified | Message or email to finance | Easy to miss; can take up to a business day |
| Customer created | Finance re-types details into Zoho Books | Typos, and duplicate customers for repeat buyers |
| Invoice created | Finance re-types deal name and amount | Amount errors; no link back to the deal |
| Sales follow-up | Salesperson asks finance about the invoice | Sales can't see invoice status in HubSpot |

## Goals

1. Create the Zoho Books invoice automatically when a deal reaches Closed Won.
2. Never create duplicate customers or invoices.
3. Let sales see the invoice number and status on the deal in HubSpot.
4. Keep credentials secure and the running cost close to zero.

## Requirements

| # | Requirement | How it is met |
|---|---|---|
| R1 | Invoice created soon after a deal is won | EventBridge runs the sync every minute |
| R2 | Existing customers are reused | Customer matched by email before creating one |
| R3 | A deal never produces two invoices | Invoice reference `HS-<deal ID>` checked before creating |
| R4 | Each invoice traceable to its deal | Deal ID stored as the invoice reference |
| R5 | Invoice visible in HubSpot | Invoice number and status written to deal properties |
| R6 | One failure doesn't stop the rest | Each deal processed independently, failures retried next run |
| R7 | No credentials in code or logs | Environment variables; query strings removed from error logs |

## Data mapping

| HubSpot | Zoho Books |
|---|---|
| Associated company name (or contact name if no company) | Customer name |
| Contact first name, last name, email | Primary contact person |
| Deal name | Invoice line item name |
| Deal amount | Invoice line item rate (quantity 1) |
| Deal ID | Invoice reference number `HS-<deal ID>` |
| `zoho_invoice_number`, `zoho_payment_status` (custom properties) | Invoice number and status |

## Key decisions

| Decision | Options considered | Choice and reason |
|---|---|---|
| Trigger | HubSpot webhook, workflow webhook, scheduled polling | **Polling.** Service Keys don't support webhooks, and workflow webhooks need a paid HubSpot tier. Polling also retries failures automatically. |
| Customer matching key | Company name, email | **Email.** Names differ between systems; an email identifies one person. |
| Hosting | Always-on server, AWS Lambda | **Lambda.** Runs only when needed and stays within the free tier at this volume. |
| Secrets | Environment variables, AWS Secrets Manager | **Environment variables** for this stage; Secrets Manager listed as the production step. |

## Out of scope (for this phase)

- Syncing payments from Zoho Books back to HubSpot
- Multiple line items and product mapping
- Tax handling
- Multi-currency deals

## Success measure

Deals reaching Closed Won get a matching Zoho Books invoice within a minute or two, with no duplicate customers or invoices, compared with a manual handoff that could take up to a business day.
