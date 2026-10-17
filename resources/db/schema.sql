-- Insurance Claims Processing System: the database schema.
--
-- One schema per service. A service reads and writes only its own schema;
-- it gets data owned by another service by calling that service's API.
-- That is why there are no foreign keys between schemas.
--
-- Safe to run more than once: it creates only what does not exist yet.

-- On a second run, don't print an "already exists, skipping" notice for every table.
SET client_min_messages = warning;

CREATE SCHEMA IF NOT EXISTS customer;
CREATE SCHEMA IF NOT EXISTS policy;
CREATE SCHEMA IF NOT EXISTS claim;
CREATE SCHEMA IF NOT EXISTS payment;
CREATE SCHEMA IF NOT EXISTS notification;

-- customer-service ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS customer.customers (
    customer_id text        PRIMARY KEY CHECK (customer_id ~ '^C-[0-9]{4}$'),
    full_name   text        NOT NULL,
    email       text        NOT NULL UNIQUE,
    status      text        NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- policy-service -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS policy.policies (
    policy_id      text          PRIMARY KEY CHECK (policy_id ~ '^P-[0-9]{4}$'),
    customer_id    text          NOT NULL,
    policy_type    text          NOT NULL CHECK (policy_type IN ('MOTOR', 'HOME', 'HEALTH', 'TRAVEL')),
    status         text          NOT NULL CHECK (status IN ('ACTIVE', 'LAPSED', 'CANCELLED')),
    coverage_limit numeric(12,2) NOT NULL CHECK (coverage_limit > 0),
    deductible     numeric(12,2) NOT NULL CHECK (deductible >= 0),
    start_date     date          NOT NULL,
    end_date       date          NOT NULL,
    CHECK (end_date > start_date)
);

CREATE INDEX IF NOT EXISTS policies_customer_id_idx ON policy.policies (customer_id);

-- claims-api ---------------------------------------------------------------

-- Claim IDs look like CLM-100001. The database creates them, so every
-- machine numbers its claims the same way.
CREATE SEQUENCE IF NOT EXISTS claim.claim_number_seq START 100001;

CREATE TABLE IF NOT EXISTS claim.claims (
    claim_id        text          PRIMARY KEY DEFAULT ('CLM-' || nextval('claim.claim_number_seq')),
    customer_id     text          NOT NULL,
    policy_id       text          NOT NULL,
    incident_date   date          NOT NULL,
    description     text          NOT NULL CHECK (char_length(description) BETWEEN 10 AND 1000),
    amount_claimed  numeric(12,2) NOT NULL CHECK (amount_claimed > 0 AND amount_claimed <= 1000000),
    approved_amount numeric(12,2) CHECK (approved_amount >= 0),
    status          text          NOT NULL CHECK (status IN ('APPROVED', 'REJECTED', 'UNDER_REVIEW', 'PAID', 'PAYMENT_FAILED')),
    reason          text          CHECK (reason IN ('CUSTOMER_NOT_ACTIVE', 'POLICY_NOT_ACTIVE',
                                                    'INCIDENT_OUTSIDE_POLICY_PERIOD', 'BELOW_DEDUCTIBLE',
                                                    'ABOVE_REVIEW_LIMIT', 'PAYMENT_ERROR')),
    payment_id      text,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    updated_at      timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS claims_customer_id_idx ON claim.claims (customer_id, created_at DESC);

-- payment-service ----------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS payment.payment_number_seq START 100001;

CREATE TABLE IF NOT EXISTS payment.payments (
    payment_id       text          PRIMARY KEY DEFAULT ('PAY-' || nextval('payment.payment_number_seq')),
    -- UNIQUE: a claim can be paid only once, even if the Redis lock is not available.
    claim_id         text          NOT NULL UNIQUE,
    customer_id      text          NOT NULL,
    amount           numeric(12,2) NOT NULL CHECK (amount > 0),
    status           text          NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
    bank_transfer_id text,
    failure_reason   text          CHECK (failure_reason IN ('BANK_TIMEOUT', 'BANK_ERROR')),
    created_at       timestamptz   NOT NULL DEFAULT now()
);

-- notification-service -----------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS notification.notification_number_seq START 100001;

CREATE TABLE IF NOT EXISTS notification.notifications (
    notification_id text        PRIMARY KEY DEFAULT ('NTF-' || nextval('notification.notification_number_seq')),
    -- UNIQUE: a message delivered twice still produces only one email.
    event_id        uuid        NOT NULL UNIQUE,
    claim_id        text        NOT NULL,
    customer_id     text        NOT NULL,
    email           text        NOT NULL,
    subject         text        NOT NULL,
    body            text        NOT NULL,
    status          text        NOT NULL CHECK (status IN ('SENT')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notifications_claim_id_idx ON notification.notifications (claim_id);
 