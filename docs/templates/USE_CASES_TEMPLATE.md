# Use Cases & User Stories — {{PROJECT_NAME}}

<!-- Template version: 1.0 | Standard: Alistair Cockburn Use Case Style / IEEE 1998 -->
<!-- STRUCTURE REFERENCE: Follow every section and UC/US format below exactly. -->

---

## Document Information

| Field | Value |
|-------|-------|
| Document Title | Use Cases & User Stories |
| Project | {{PROJECT_NAME}} |
| Version | {{VERSION}} |
| Date | {{DATE}} |

---

## 1. Actors

List every actor that interacts with the system. An actor is a person, system, or
role — not an individual user.

| Actor ID | Name | Description | Primary Goals |
|----------|------|-------------|---------------|
| A-01 | (e.g. End User) | — | Perform core workflows |
| A-02 | (e.g. Administrator) | — | Manage users and configuration |
| A-03 | (e.g. External System) | — | Consume the API |

---

## 2. Use Case Summary

| UC ID | Name | Primary Actor | Priority |
|-------|------|--------------|---------|
| UC-001 | — | — | High |
| UC-002 | — | — | High |
| UC-003 | — | — | Medium |

---

## 3. Detailed Use Cases

> **Format per use case (FILL IN ALL FIELDS):**

### UC-001: [Action Verb] + [Subject — derived from endpoint or model name]

| Field | Value |
|-------|-------|
| **ID** | UC-001 |
| **Name** | [Verb + Noun] |
| **Primary Actor** | A-01 |
| **Secondary Actors** | None / A-02 |
| **Priority** | High / Medium / Low |
| **Implements** | `METHOD /api/path` → `handler_function()` |

#### Preconditions
- System is running and accessible.
- — (list any additional preconditions)

#### Trigger
Describe what initiates this use case (user action, scheduled event, external message).

#### Main Success Flow

| Step | Actor Action | System Response |
|------|-------------|----------------|
| 1 | Actor navigates to / calls `METHOD /path` | System authenticates request |
| 2 | Actor provides required input | System validates input |
| 3 | — | System persists data and returns success response |

#### Alternative Flows

**4a. Validation Failure**
1. At step 2, if required fields are missing:
2. System returns `400 Bad Request` with field-level error messages.
3. Use case returns to step 2.

**4b. Resource Not Found**
1. At step 3, if the requested resource does not exist:
2. System returns `404 Not Found`.
3. Use case ends (failure).

#### Postconditions
- **Success:** State of the system after successful completion.
- **Failure:** State of the system after failure.

#### Business Rules
- BR-1: ...

---

### UC-002: [Next Use Case]

*(Repeat UC template above for each major feature area)*

---

### UC-003: [Authentication / Login]

| Field | Value |
|-------|-------|
| **ID** | UC-003 |
| **Name** | Authenticate User |
| **Primary Actor** | A-01 (End User) |
| **Priority** | High |
| **Implements** | `POST /api/auth/login` → `login()` |

#### Preconditions
- User has a registered account.

#### Trigger
User submits credentials via login form or API call.

#### Main Success Flow

| Step | Actor Action | System Response |
|------|-------------|----------------|
| 1 | User submits `username` + `password` | System validates credentials against database |
| 2 | — | System issues a signed JWT / session token |
| 3 | User stores the token | User includes token in `Authorization: Bearer <token>` header |

#### Alternative Flows

**1a. Invalid Credentials**
1. System returns `401 Unauthorized`.
2. After 5 consecutive failures, account is temporarily locked.

---

## 4. User Stories

> **Format:** **US-NNN**: As a [actor], I want to [action mapped to a real endpoint or feature] so that [business benefit].

### High Priority

**US-001**: As a [Actor], I want to [perform primary action via `METHOD /path`] so that [benefit].

**Acceptance Criteria:**
- [ ] Given [precondition], when [action], then [expected system response].
- [ ] The response body conforms to the documented schema.
- [ ] Response time is under 500 ms for typical input.

---

**US-002**: As a [Actor], I want to [action] so that [benefit].

**Acceptance Criteria:**
- [ ] ...
- [ ] ...

---

### Medium Priority

**US-010**: As a [Actor], I want to [action] so that [benefit].

**Acceptance Criteria:**
- [ ] ...

---

### Low Priority

**US-020**: As a [Actor], I want to [action] so that [benefit].

**Acceptance Criteria:**
- [ ] ...

---

## 5. Feature–Endpoint Traceability Matrix

| Feature | Endpoint | Handler | Use Case | User Story |
|---------|----------|---------|---------|-----------|
| — | `GET /api/resource` | `handler()` | UC-001 | US-001 |
| — | `POST /api/resource` | `create()` | UC-002 | US-002 |

---

## 6. Out of Scope

List workflows or actor types that are explicitly NOT covered by this system:

- ...
- ...

---

*End of Use Cases & User Stories*
