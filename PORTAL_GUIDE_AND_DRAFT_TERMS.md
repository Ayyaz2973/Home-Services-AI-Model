# HomeServe Portal Guide and Draft Terms

> **Important:** This is an operational guide and a plain-language draft, not
> legal advice. Replace the bracketed details and have a lawyer familiar with
> applicable Pakistani law review the terms before relying on or publishing
> them. The app now displays draft terms and privacy notices and records the
> accepted draft versions with new customer requests and technician applications.
> This does not replace a complete privacy policy or local legal review.

## Portal overview

HomeServe connects customers who request household services with technicians
managed by the service administrator. The portal has three areas:

- Customer website: `http://localhost:8000/`
- Technician portal: `http://localhost:8000/tech`
- Admin portal: `http://localhost:8000/admin`

When running locally, keep the command window opened by `run.bat` running.
For a hosted service, use HTTPS, restrict access to the Admin portal, and
replace the development credentials before inviting real users.

## Cloud test deployment: Vercel portal + Railway backend

The frontend is served by Vercel. The FastAPI backend runs as a long-lived
Railway service because the app needs persistent SQLite and private-file
storage, plus a background job sweeper. Vercel forwards `/api/*` requests to
the backend; browser requests remain same-origin with the portal.

1. Push the project to a private GitHub repository. The ignore files exclude
   the local virtual environment, database, uploads, and environment files.
2. In Railway, create a project from that repository. Attach a persistent
   volume mounted at `/data`.
3. Set these Railway variables before the first deploy:
   - `DATABASE_PATH=/data/homeserve.db`
   - `PRIVATE_STORAGE_DIR=/data/private_uploads`
   - `ADMIN_KEY` to a unique, long secret (not `admin123` or `change-me-now`)
   - `DOCUMENT_MAX_BYTES=4194304`
   - `PHOTO_MAX_BYTES=4194304`
   - `COMMISSION_PCT=10`
   - `ASSIGN_TIMEOUT_MIN=10`
4. Generate a public Railway domain and wait for `https://<railway-domain>/api/meta`
   to return JSON. The database and private uploads persist only if the Railway
   volume is mounted at `/data`.
5. In Vercel, import the same repository. In **Project Settings → Build and
   Deployment**, set **Root Directory** to `static` and **Framework Preset**
   to **Other**. This is required: the repository root contains `main.py`,
   which Vercel otherwise detects and starts as a FastAPI function. The static
   project root contains the portal pages and the `/api` proxy, not the Python
   backend. Set `BACKEND_URL` to the Railway HTTPS origin only, for example
   `https://your-service.up.railway.app` (do not add `/api`). Leave the build
   and output-directory overrides empty, then redeploy.
6. Test the Vercel URL at `/`, `/tech`, and `/admin`. Admin access uses the
   Railway `ADMIN_KEY`. Add test technicians through Admin or submit an
   application; production Railway instances do not receive the local demo
   technicians.

The Vercel preview limits each file to 4 MiB and all documents in a single
technician application to 4 MiB combined, to stay below the serverless request
body limit. AI triage uses the built-in fallback unless a reachable Ollama
service is separately configured. Use test-only contact details and documents;
this setup is for functionality testing, not a production launch.

## Customer guide

### Request a service

1. Open the customer website.
2. Enter your name, phone number, full address, and a description of the
   problem. Choose an available fixed-price service to see its service price
   and visit fee, choose a category, or leave it on auto-detect.
3. Review and accept the linked draft customer terms and privacy notice.
   Submit the request. The portal creates a tracking link for that request.
   Save the link; customers do not currently create an account or password.
4. Open the tracking link later to check the request status and progress.
   Keep the link private: it grants access to this request's status and photos.

### Estimates, extra work, and job photos

- A listed service's price and visit fee are saved with the request as an
  estimate. It is not an online payment or an estimate for work outside its
  stated scope.
- If extra work is needed, the technician must propose its price and reason.
  Approve or decline the proposal on the tracking page before extra work is
  done. A pending proposal blocks job confirmation.
- Technicians can upload up to five before-work and five after-work photos
  for an accepted job. At least one photo of each phase is required before a
  technician can mark the job complete. Photos are shown on the request's
  private tracking page and are available to the assigned technician and Admin.
- After work is marked done, confirm or report a problem within 24 hours. If
  the customer does neither, the app automatically confirms the job and
  updates its payment ledger. It does not automatically collect or transfer
  real money.

### Track the technician

When a technician has accepted the job, their tracking page can show a map if
they have explicitly started sharing their live location. The map appears on
the customer tracking page and refreshes as new location updates arrive.

Location sharing is optional. The map may be unavailable if the technician
has not started sharing, their device/browser cannot get a location fix, or
the location has not been refreshed for two minutes. GPS can be inaccurate;
a map marker is not proof that the technician has arrived at the address.
Maps are provided by OpenStreetMap, not by Yango, Uber, or Careem.

### Confirm, report a problem, or cancel

- After the technician marks the work complete, review the amount, work note,
  and before/after photos on the tracking page.
- Confirm the work and choose a rating if you are satisfied. The app then
  records the payment as released to the technician.
- If there is a problem, use **Report a problem** instead of confirming.
- If no problem is reported within 24 hours of completion, the system
  automatically confirms the job and records a release in its payment ledger.
  This does not charge a card or wallet or move money.
- The app allows cancellation while a request is new, unassigned, or assigned.
  If the technician has accepted, contact the administrator.

The current app does not take card or wallet payments. Its payment ledger
records amounts, collection, and payouts for the administrator to handle
outside the app.

## Technician guide

### Account setup and sign-in

1. Open `/tech` and choose **Apply to join**.
2. Submit your contact and service details, create a PIN, upload the required
   CNIC front/back and profile photo, and optionally attach skill or police
   documents. Accept the linked draft provider terms and privacy notice.
3. Wait for an administrator to review the application and documents. An
   application is not an active account; sign-in is enabled only after approval.

Administrators may still create technician accounts manually in the Admin
portal. Applications and uploaded documents are stored privately on the
server, but the operator must secure server access and backups and define a
retention/deletion policy before using real identity documents.

### Availability and jobs

1. Sign in using the phone number and PIN provided by the administrator.
2. Turn **I am available for new jobs** on when ready to receive work and off
   when unavailable.
3. Review a new job and choose **Accept** or **Decline**.
4. After accepting, record the GPS arrival check-in when you reach the job.
   It saves the device's reported coordinates and accuracy but does not
   independently verify your location against the service address.
5. Contact the customer as needed and perform the agreed work.
6. Upload at least one before-work and one after-work photo. If extra work is
   needed, submit its price and reason and wait for customer approval.
7. Enter the amount charged and a brief work note, then choose **Mark work
   done**. The customer can confirm or report a problem within 24 hours.
8. Use **Logout** when finished using the portal.

### Optional live location sharing

For an accepted job, choose **Share my live location** and grant the
browser's location permission. The customer's tracking page and the
administrator can then see the technician's recent position on a map.
Location sharing is off until the technician starts it. Choose **Stop
sharing** to end it, or mark the job done; either action clears the saved
location. Location updates that stop refreshing are hidden after two minutes
and are cleaned up by the server.

Keep the technician portal open while sharing. Browser background location
tracking is not provided. Browsers generally require HTTPS for location on a
hosted website; `localhost` is allowed for local development. A GPS position
does not itself confirm arrival or verify work at the address.

The required one-time GPS arrival check-in is separate from optional live
sharing. A check-in is required before a job can be marked done.

## Administrator guide

1. Start the service with `run.bat`, then open `/admin`.
2. Sign in with the key configured as `ADMIN_KEY` in the launch environment.
   Change the development key before allowing access from other devices.
3. Use the dashboard tabs:
   - **Requests:** review jobs, assign a technician, request auto-assignment,
     close eligible requests, and contact customers through WhatsApp.
   - **Technicians:** add or edit a technician, change their phone, service,
     area, or PIN; review provider applications and documents; approve or
     reject applicants; and remove or restore an account.
   - **Price catalogue:** add and edit fixed-price services, visit fees,
     descriptions, and customer visibility.
   - **Payments:** review the ledger, mark customer funds collected, and record
     a technician payout reference.
4. In Requests, a **View live map** link is available for a fresh location
   shared during an accepted job.

**Remove** archives and disables the technician account; it does not erase
past job or payment history. **Restore** reactivates the account. An archived
technician must sign in and mark themselves available before receiving new
work.

The portal's payment ledger is not a payment processor. Verify and execute
transfers through the agreed external payment method, then record the
collection or payout in the ledger.

## Draft customer terms of service

**Provider:** [Legal business name]  
**Location:** [Business address, Islamabad, Pakistan]  
**Contact:** [Support email and phone]  
**Effective date:** [Date]

### 1. Using the service

HomeServe provides a platform for submitting home-service requests and
coordinating with independent service professionals. Submitting a request
does not guarantee that a technician is available, that a particular arrival
time can be met, or that a job will be accepted.

Customers must provide accurate contact details, address, and job
description, and must have permission to request work at the service
location. Customers should not submit false, abusive, unlawful, or
emergency-threatening requests through the platform.

### 2. Estimates, extra work, and payment

An active catalogue item displays the saved service price plus visit fee as a
request estimate. The estimate applies only to the described catalogue scope.
Extra work requires a price proposal and customer approval in the portal
before it proceeds. Before work begins, the customer and technician should
agree on the complete scope, materials, and any charge not covered by the
estimate.

The technician must record an arrival GPS check-in and upload at least one
before-work and one after-work photo before marking a job complete. GPS
coordinates are device-reported and do not independently verify the service
address. Customers have 24 hours after completion to confirm or report a
problem. If no problem is reported, the app automatically confirms the job
and updates its internal ledger; it does not transfer real money.

Unless separately agreed in writing, the customer pays the technician or
service provider using the payment method they agree to. The app does not
currently collect payments, hold funds, or issue refunds.

### 3. Cancellations and complaints

Customers may cancel through the tracking page while the request is new,
unassigned, or assigned. For an accepted request or a dispute about completed
work, contact [support contact]. Customers should report concerns promptly
and provide accurate details so the parties can attempt to resolve them.

### 4. Technician location and privacy

Live location is optional and is shared only after the technician starts
sharing it for an accepted job. The current portal makes a fresh location
visible to the customer holding that request's tracking link and to the
administrator. The portal hides locations after two minutes without an
update and deletes location records when sharing is stopped or the job ends.
GPS accuracy depends on the device, browser, and surroundings.

The customer must treat the tracking link as private and must not publish,
forward, or use it to monitor or contact a technician for purposes unrelated
to the service request. Personal information must be handled in accordance
with the provider's published privacy notice: [Privacy notice URL].

### 5. Safety and responsibility

Customers should provide safe, lawful access to the property and disclose
known hazards relevant to the work. HomeServe should identify whether
technicians are independent providers or employees and explain how
qualifications, insurance, complaints, and service guarantees are handled:
[Provider to complete after legal review].

Nothing in these terms removes a right or remedy that cannot legally be
excluded. Any limits on liability, warranties, or dispute resolution must be
reviewed and completed by local counsel before publication.

### 6. Changes and contact

The provider may update these terms by publishing a revised version with an
updated effective date. Questions or complaints should be sent to
[support email/phone].

## Draft technician / service-professional terms

**Provider:** [Legal business name]  
**Location:** [Business address, Islamabad, Pakistan]  
**Contact:** [Support email and phone]  
**Effective date:** [Date]

### 1. Eligibility and account setup

The current portal does not accept self-registration. A prospective
technician must apply to [provider/contact]. The administrator reviews the
application and creates an account if approved. The provider should specify
any required identity, qualification, licensing, reference, or insurance
checks before onboarding: [Provider to complete].

Technicians must provide accurate name, phone number, service category, and
area information, promptly report changes, and keep their sign-in PIN
confidential. Accounts are for the named technician only. The technician
must promptly notify the administrator if a phone or PIN is lost or
compromised.

### 2. Availability and job conduct

Technicians control the availability setting and should only mark themselves
available when they can reasonably respond to requests. Accepting a job
creates an expectation to contact the customer, confirm the scope and price
before beginning work, arrive as agreed, perform the work competently and
lawfully, and communicate delays or problems promptly.

Technicians must not misrepresent qualifications, request unrelated
customer information, discriminate, harass, threaten, or engage in
fraudulent conduct. They must follow applicable safety rules and any
professional licensing requirements. The provider should define its
screening, insurance, safety, complaint, and service-quality standards:
[Provider to complete].

### 3. Charges, commission, and payouts

Before starting work, the technician and customer should agree on the
service charge, materials, and additional charges. The technician must enter
the actual agreed amount and an accurate completion note in the portal.

The service commission is [agreed percentage and calculation]. The current
development launcher is configured to calculate a **10% commission**; the
provider must confirm the actual rate, taxes, deductions, collection
responsibility, payout timing, and dispute/refund adjustments in a signed
agreement before using this draft. The app records amounts and payout
references but does not transfer funds. Payments are settled outside the app
using the agreed method.

### 4. Live location

Location sharing is optional. For an accepted job, the technician may choose
to share device location with the customer for that request and with the
administrator. The technician can stop sharing at any time; sharing also
stops when the job is marked done. Fresh location is hidden after two minutes
without an update, and the stored location is cleaned up when it becomes
stale or the job ends.

The technician must use location sharing only for the accepted service job,
not falsify or manipulate location, and not use the customer's tracking link
or location information for unrelated purposes. GPS data can be inaccurate
and is not, by itself, proof of arrival, attendance, or completed work.

### 5. Records, privacy, and customer information

Technicians may use customer contact, address, and job details only to
coordinate and perform the accepted request and meet lawful record-keeping
duties. They must not copy, sell, disclose, or retain customer information
for unrelated purposes. The provider should publish its privacy notice,
retention rules, and process for data requests: [Privacy notice URL/contact].

### 6. Account suspension and termination

The administrator may deactivate an account for suspected fraud, unsafe or
unlawful conduct, repeated failure to meet accepted commitments, misuse of
customer information, or another material breach, subject to applicable law
and any required notice or appeal process. Deactivation prevents sign-in and
new assignments. Historical job and payment records may remain in the
service database for operational, accounting, or legal purposes.

The provider should complete applicable termination, final-payment, appeal,
and dispute procedures with local counsel.

### 7. Relationship and legal review

The provider must clearly state whether technicians are employees,
contractors, or independent businesses and document the actual relationship.
This draft does not decide that status. Nothing in these terms removes a
right that cannot legally be excluded.

Applicable law, liability, indemnity, insurance, tax, dispute-resolution,
and jurisdiction provisions must be completed by local counsel before these
terms are presented for acceptance.

## Before publishing or onboarding real users

- Replace all bracketed placeholders and confirm the provider's legal name,
  support contact, privacy policy, and applicable legal venue.
- Have the customer and technician terms reviewed by a qualified Pakistani
  lawyer.
- Replace the draft text with counsel-approved, versioned terms and a complete
  privacy notice before collecting real users' data.
- Set and document secure retention, deletion, backup, and privacy-request
  procedures for uploaded identity documents and job photos.
- Review each applicant's documents and verify eligibility out-of-band before
  approval; the app does not perform CNIC, police, or skill verification.
- Change development admin credentials and demo technician PINs, use HTTPS
  for hosted deployments, restrict Admin access, and back up `homeserve.db`.
