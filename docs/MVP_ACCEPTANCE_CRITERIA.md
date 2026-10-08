# HomeServe Masterpiece Architecture: MVP Acceptance Criteria

> **Document Status:** Authoritative MVP Acceptance Baseline  
> **Source Version:** Sections 31–49 of the Masterpiece Architecture Specification  
> **Scope:** End-to-end controlled and auditable home services transaction

---

# 31. Explicit MVP Acceptance Criteria

The MVP is accepted only when the complete service transaction can be executed end-to-end without manual database manipulation or developer intervention.

## A. MVP Definition

The MVP must prove this core transaction:

**Customer request → AI triage → quotation/scope → customer approval → provider assignment → provider acceptance → job execution → evidence → completion → customer confirmation → commission calculation → provider entitlement → payout record → rating → audit trail.**

The existing v1 already demonstrates the foundation of request intake, AI classification, assignment, acceptance, work completion, customer confirmation and commission handling. 

The MVP expands this into a controlled and auditable transaction.

---

# 32. MVP Acceptance Test Matrix

## AC-01 — Customer Registration

**Given:** a new customer

**When:** the customer registers

**Then:**

* account is created successfully
* customer can log in
* customer can create at least one property
* customer can edit their contact information
* customer cannot access provider/admin functions

**PASS:** all four actions work without database intervention.

---

## AC-02 — Property Management

Customer must be able to:

* create property
* enter address/area
* add access instructions
* edit property
* select property when creating a job

**PASS:** a job can be linked to the correct property and the property remains associated with the job history.

---

# 33. Service Request Acceptance

## AC-03 — Create Service Request

Customer can submit:

* service description
* property
* preferred time
* urgency
* optional photos

The system creates a unique request/job ID.

**PASS:** request appears in the customer portal and admin operations dashboard.

---

## AC-04 — AI Triage

The system must:

* classify service category
* determine urgency
* create a short summary

If AI is unavailable, the system must use the existing fallback mechanism rather than failing the request. The current playbook explicitly defines AI triage with keyword fallback. 

**PASS:** every valid request receives a category and urgency.

---

# 34. Provider Acceptance

## AC-05 — Provider Verification

A provider cannot receive production jobs until required verification information is approved.

Minimum MVP provider record:

* name/company
* phone
* service category
* service area
* skills
* availability
* verification status
* rating/performance record

**PASS:** an unverified provider cannot be assigned an active production job.

---

## AC-06 — Provider Login

Provider/technician must be able to:

* log in securely
* see assigned/available jobs
* accept a job
* decline a job
* view job status
* view earnings

**PASS:** provider actions immediately update the job state.

---

## AC-07 — Assignment

The assignment engine must select only providers who:

* match the required service
* are available
* are not already busy
* are eligible to receive work

Same-area preference and rating/job-count logic should be supported as established by the current baseline. 

**PASS:** an eligible provider receives the assignment.

---

## AC-08 — Decline/Reassignment

When a provider declines:

* job becomes available for reassignment
* next eligible provider can receive it
* previous provider is recorded as having declined

The current baseline explicitly requires reassignment after a decline. 

**PASS:** no job becomes permanently stuck because one provider declined.

---

## AC-09 — Assignment Timeout

If a provider does not respond within the configured period:

* assignment expires
* job is automatically eligible for reassignment
* timeout is recorded in audit history

The current baseline uses 10 minutes as the initial rule. 

**PASS:** timeout can be demonstrated without manually changing the database.

---

# 35. Scope & Pricing

## AC-10 — Service Catalogue

Admin can create:

* service category
* service
* base price
* unit
* checklist
* warranty period

Customer/provider cannot directly modify the authoritative catalogue price.

**PASS:** a quotation can be generated from catalogue data.

---

## AC-11 — Quotation

A quotation must show:

* service
* scope
* quantity
* price
* applicable fees
* discount if applicable
* total
* validity
* warranty where applicable

**PASS:** customer can clearly see what they are approving and the total amount.

---

## AC-12 — Customer Approval

Customer must explicitly approve the quotation before execution of a priced job.

Approval must record:

* customer
* timestamp
* quotation/version
* approved amount

**PASS:** the system can prove exactly which quotation the customer approved.

---

# 36. Work Execution

## AC-13 — Work Order

Once approved, the system creates a work order containing:

* approved scope
* approved price
* property
* customer
* assigned provider
* deadline/SLA
* checklist
* evidence requirements

**PASS:** provider cannot execute outside the authorized work order without an appropriate variation.

---

## AC-14 — Customer Information Protection

Before acceptance, the provider receives only the information necessary to evaluate/accept the assignment.

After acceptance, authorized customer contact/address information becomes available.

This preserves the privacy behavior established in the baseline. 

**PASS:** unauthorized customer information is not visible before the required state.

---

## AC-15 — Check-In

Provider must be able to check in to the job.

System records:

* provider
* job
* date/time
* location/evidence where available

**PASS:** a job cannot be marked completed without the required execution state.

---

## AC-16 — Before Evidence

For applicable services, provider must upload required before-work photos.

**PASS:** the system prevents completion where mandatory before evidence is missing.

The baseline already identifies mandatory before/after photos as a core dispute-prevention mechanism. 

---

## AC-17 — Checklist

Provider must complete the service-specific checklist.

Example:

```text
AC Service
☐ Filter cleaned
☐ Gas pressure checked
☐ Drain tested
```

**PASS:** required checklist items must be completed before work can be submitted as done.

---

# 37. Extra Work

## AC-18 — Extra Work Request

Provider can request additional work containing:

* description
* reason
* price
* evidence/photo where required

The original job price remains unchanged until approval.

**PASS:** submitting extra work does not automatically charge the customer.

---

## AC-19 — Extra Work Approval

Customer can:

* approve
* reject

Only approved extra work becomes part of the authorized scope.

This directly implements the baseline rule that extra work requires in-app customer approval. 

**PASS:** rejected/unapproved extra work cannot be included in the final customer charge.

---

# 38. Completion

## AC-20 — After Evidence

Provider uploads required after-work evidence.

**PASS:** applicable jobs cannot be submitted as completed without mandatory evidence.

---

## AC-21 — Work Done

Provider can mark work done only when:

* required checklist is complete
* required evidence exists
* required check-in exists
* required variation approvals are complete

**PASS:** incomplete jobs cannot bypass these controls through the UI/API.

---

# 39. Customer Confirmation

## AC-22 — Customer Confirmation

Customer can:

* confirm work
* rate provider
* report a problem

The job must transition to the appropriate final state.

The baseline already uses customer confirmation as the event that releases payment. 

**PASS:** provider entitlement is not finalized before the required confirmation state.

---

## AC-23 — Customer Dispute

Customer can report a problem instead of confirming.

System must:

* create dispute
* preserve original job state/history
* preserve evidence
* prevent normal settlement until dispute resolution rules allow it
* notify the appropriate operator

**PASS:** disputed jobs cannot silently become paid-out jobs.

---

# 40. Money & Commission

## AC-24 — Final Amount Calculation

The system must calculate:

```text
Base amount
+ approved variations
- discounts
+ applicable fees/taxes
= customer final amount
```

Only authorized amounts may enter the calculation.

---

## AC-25 — Commission Calculation

System calculates commission according to the configured commission rule.

The existing baseline uses 10% as the default example. 

**PASS:** commission is automatically calculated and stored against the transaction.

---

## AC-26 — Provider Entitlement

After successful confirmation/settlement:

```text
Provider Entitlement
= Authorized Provider Amount
- applicable adjustments
```

The provider's balance must be represented by ledger transactions rather than an unexplained editable number.

**PASS:** every provider balance change has a source transaction.

---

## AC-27 — Payout Record

Admin can create a payout record containing:

* provider
* amount
* payment method
* date
* reference number
* status

The baseline already specifies JazzCash, Easypaisa or bank payout recording. 

**Important MVP limitation:** actual automated movement of money is **not required** unless a real payment provider has been integrated. The current baseline explicitly states that v1 tracks money but does not move it. 

**PASS:** manual payout can be accurately recorded and reconciled.

---

# 41. Invoice

## AC-28 — Customer Invoice

A completed payable job must produce an invoice/receipt containing:

* invoice number
* customer
* property
* services
* variations
* total
* payment status
* date

**PASS:** invoice matches the approved transaction total.

---

# 42. Rating & Performance

## AC-29 — Rating

Customer can rate the completed service.

Rating becomes part of provider performance history.

**PASS:** rating is associated with the correct job/provider and cannot be edited by the provider.

---

## AC-30 — Provider Performance

System calculates at least:

* rating
* acceptance rate
* completion rate
* cancellation rate
* dispute rate

These measures align with the performance direction established in the playbook. 

**PASS:** administrator can view provider performance without manually calculating it.

---

# 43. Dispute & Warranty

## AC-31 — Dispute Evidence Package

When a dispute opens, the administrator must be able to see in one place:

```text
Original Request
Approved Scope
Quotation
Variations
Provider
Assignment History
Check-In
Before Photos
Checklist
Messages/Notes
After Photos
Completion
Customer Complaint
Payment Status
```

**PASS:** operator can investigate without reconstructing the case manually from separate systems.

---

## AC-32 — Warranty Claim

Customer can create a warranty/revisit request where the service has a valid warranty.

System links the claim to the original job.

The current playbook proposes a 7-day repair complaint flow, while also indicating that thresholds should ultimately be based on real data.  

**PASS:** warranty eligibility is determined from the original service's configured warranty period.

---

# 44. Audit & Security

## AC-33 — Audit Trail

The following actions must create audit records:

* login/security changes
* provider verification
* assignment
* acceptance/decline
* quotation changes
* customer approval
* extra-work approval
* check-in
* evidence upload
* work completion
* customer confirmation
* dispute creation
* dispute resolution
* commission calculation
* payout
* administrative overrides

**PASS:** administrator can reconstruct the lifecycle of any job.

---

## AC-34 — Role Isolation

Test these roles:

```text
Customer
Provider
Technician
Admin
```

Each must see only authorized functions/data.

**PASS:** attempting unauthorized API/UI actions produces a controlled authorization failure.

---

# 45. Operational Reliability

## AC-35 — No Stuck Jobs

The system must identify jobs that remain too long in:

* unassigned
* assigned
* accepted
* in-progress
* awaiting customer confirmation

**PASS:** operations dashboard shows overdue/stuck jobs.

---

## AC-36 — Failure Recovery

If the AI service is unavailable:

* request submission still works
* fallback classification operates
* job can still enter the workflow

This explicitly preserves the current v1 fallback design. 

---

# 46. MVP End-to-End Acceptance Test

The MVP is **NOT accepted** until this complete scenario passes.

### Test Job

Customer submits:

> "Kitchen ka nal leak ho raha hai."

Area:

> F-10

The current playbook uses this exact scenario as its basic test. 

### Required result

```text
1. Customer creates request
2. System identifies plumbing
3. System determines urgency
4. Eligible plumber is identified
5. Provider receives offer
6. Provider accepts
7. Provider receives authorized customer details
8. Work order is created
9. Provider checks in
10. Before photo uploaded
11. Plumbing checklist completed
12. Provider completes work
13. After photo uploaded
14. Customer sees completed work
15. Customer confirms
16. Customer rates provider
17. Final amount is calculated
18. Commission is calculated
19. Provider entitlement is created
20. Admin records payout
21. Invoice is generated
22. Complete audit trail exists
```

**PASS CONDITION:**

> The entire transaction completes without manually editing the database, and every financial, operational and approval event can be traced back to the responsible actor.

---

# 47. MVP Quality Gates

The MVP must also satisfy these non-functional gates.

### Security

* No default production admin credential
* No demo provider credentials in production
* Authentication required
* Role permissions enforced
* Sensitive customer information protected

The current playbook specifically requires changing the admin key and deleting demo technicians before real use. 

### Data Integrity

* No negative unexplained balances
* No payout without an entitlement
* No provider entitlement without valid settlement
* No extra charge without approval
* No completion without required evidence
* No duplicate payout

### Usability

A normal customer should be able to request a service without administrator assistance.

A provider should be able to accept and complete a job from the provider interface without administrator assistance.

An administrator should be able to resolve an ordinary job without editing the database.

### Reliability

The system must survive:

* AI unavailable
* provider decline
* provider timeout
* customer dispute
* incomplete evidence
* cancelled job
* failed notification

without corrupting the transaction.

---

# 48. MVP "Done" Definition

The HomeServe Masterpiece MVP is officially **DONE** only when:

**☑ Customer can request**

**☑ AI/fallback can classify**

**☑ Provider can be verified**

**☑ Provider can be assigned**

**☑ Provider can accept/decline**

**☑ Scope and price can be approved**

**☑ Provider can execute**

**☑ GPS/check-in is recorded**

**☑ Required evidence is captured**

**☑ Checklist is enforced**

**☑ Extra work requires approval**

**☑ Customer can confirm or dispute**

**☑ Commission is calculated**

**☑ Provider entitlement is calculated**

**☑ Payout can be recorded**

**☑ Invoice can be generated**

**☑ Rating is captured**

**☑ Warranty can be initiated**

**☑ Dispute package is complete**

**☑ Audit trail is complete**

**☑ Roles and permissions work**

**☑ AI failure does not stop the business**

**☑ The complete end-to-end test passes**

---

# 49. MVP Release Rule

There should be **no "almost ready" production release**.

Release only when:

> **100% of Critical acceptance criteria pass.**

Critical criteria are:

**AC-03, AC-04, AC-07, AC-08, AC-10, AC-11, AC-12, AC-13, AC-15, AC-16, AC-17, AC-18, AC-19, AC-20, AC-21, AC-22, AC-23, AC-24, AC-25, AC-26, AC-27, AC-31, AC-33, AC-34 and AC-46.**

Any failure in these means the MVP remains **NOT READY**.

The objective is not merely to demonstrate that the software works.

The objective is to demonstrate that **HomeServe can safely operate a real service transaction.**
