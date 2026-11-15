# Requests and hand-offs

The product manager uses this page to ask the engineering manager and the solution architect for work.
There are two ways to do it: a request and a hand-off.

## File a request

1. Open **Requests** and select **New request**.
2. Pick one type:
   - **Technical dependency** goes to the solution architect.
   - **New feature** goes to the engineering manager.
3. Give a title and a short description. You can link a report item or a component.
4. Select **Send request**.

The role that owns the request gets a notification and an entry in **Needs a person**. The other role
always gets the request too, marked **For your information**, so both see every request.

## Answer a request

Only the owner sets the status:

- **Accepted**
- **Needs more info**
- **Declined**

Each answer needs a one-line reply. The product manager gets a notification with the status and the reply.

## Hand over an item

On a forecast item or an investigation report, the product manager selects **Hand to engineering manager**
or **Hand to solution architect** and adds a short note. ATLAS records the hand-off in the project's audit
record, notifies the receiver and adds an entry to **Needs a person**.

The engineering manager decides how the work is done. There are exactly two choices:

- **Delegated to coding agent**
- **Delegated to on-call engineer**

Until then the item shows **Awaiting your decision** to the engineering manager. Everyone sees the same
label, so the product manager and the solution architect can follow the hand-off. The solution architect
accepts the hand-offs sent to them.

## On the home page

- The engineering manager and the solution architect see **Hand-offs waiting** and **Requests from
  product**. The request tile counts new requests, the ones you own and the ones sent for information.
- The product manager sees **My requests** with their status, and how many hand-offs are still waiting.

## Rules

- Only the product manager files requests and hands over items.
- Requests and hand-offs belong to one project. People in another project cannot see them.
- Sending the same request twice with the same key records it once.
