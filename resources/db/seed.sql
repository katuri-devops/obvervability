-- Insurance Claims Processing System: the seed data.
--
-- Ten customers and sixteen policies, the same on every machine.
-- Policy dates are counted from the day this script runs, so the active
-- policies are active whenever the course takes place.
--
-- Safe to run more than once: it puts every seeded row back to the values
-- below. Claims, payments and notifications are not touched.

INSERT INTO customer.customers (customer_id, full_name, email, status)
VALUES
    ('C-1001', 'Elena Markovic', 'elena.markovic@example.com', 'ACTIVE'),
    ('C-1002', 'Tomas Herrera',  'tomas.herrera@example.com',  'ACTIVE'),
    ('C-1003', 'Ingrid Solberg', 'ingrid.solberg@example.com', 'ACTIVE'),
    ('C-1004', 'Kwame Mensah',   'kwame.mensah@example.com',   'ACTIVE'),
    ('C-1005', 'Hana Sato',      'hana.sato@example.com',      'ACTIVE'),
    ('C-1006', 'Liam Byrne',     'liam.byrne@example.com',     'ACTIVE'),
    ('C-1007', 'Sofia Rossi',    'sofia.rossi@example.com',    'ACTIVE'),
    ('C-1008', 'Daniel Novak',   'daniel.novak@example.com',   'ACTIVE'),
    ('C-1009', 'Amara Okafor',   'amara.okafor@example.com',   'SUSPENDED'),
    ('C-1010', 'Lukas Becker',   'lukas.becker@example.com',   'ACTIVE')
ON CONFLICT (customer_id) DO UPDATE SET
    full_name = EXCLUDED.full_name,
    email     = EXCLUDED.email,
    status    = EXCLUDED.status;

-- start and end are days from today: -180 means 180 days ago.
INSERT INTO policy.policies
    (policy_id, customer_id, policy_type, status, coverage_limit, deductible, start_date, end_date)
SELECT policy_id, customer_id, policy_type, status, coverage_limit, deductible,
       current_date + start_day, current_date + end_day
FROM (VALUES
    ('P-2001', 'C-1001', 'MOTOR',  'ACTIVE',     20000.00,  500.00, -180,  185),
    ('P-2002', 'C-1001', 'HOME',   'ACTIVE',    150000.00, 1000.00, -300,   65),
    ('P-2003', 'C-1002', 'HEALTH', 'ACTIVE',     50000.00,  250.00,  -90,  275),
    ('P-2004', 'C-1003', 'TRAVEL', 'ACTIVE',      5000.00,  100.00,  -30,  335),
    ('P-2005', 'C-1003', 'MOTOR',  'LAPSED',     15000.00,  500.00, -200,  165),
    ('P-2006', 'C-1004', 'HOME',   'ACTIVE',    120000.00, 1000.00, -150,  215),
    ('P-2007', 'C-1005', 'HEALTH', 'ACTIVE',     40000.00,  250.00,  -60,  305),
    ('P-2008', 'C-1005', 'TRAVEL', 'CANCELLED',   3000.00,  100.00, -100,  265),
    ('P-2009', 'C-1006', 'MOTOR',  'ACTIVE',     25000.00,  750.00, -240,  125),
    ('P-2010', 'C-1007', 'HOME',   'ACTIVE',    200000.00, 1500.00, -120,  245),
    ('P-2011', 'C-1007', 'TRAVEL', 'ACTIVE',      4000.00,  100.00, -400,  -35),  -- period has ended
    ('P-2012', 'C-1008', 'HEALTH', 'ACTIVE',     60000.00,  500.00, -210,  155),
    ('P-2013', 'C-1009', 'MOTOR',  'ACTIVE',     18000.00,  500.00,  -80,  285),
    ('P-2014', 'C-1010', 'HOME',   'ACTIVE',     90000.00, 1000.00, -270,   95),
    ('P-2015', 'C-1010', 'MOTOR',  'ACTIVE',     22000.00,  600.00,  -45,  320),
    ('P-2016', 'C-1002', 'TRAVEL', 'ACTIVE',      6000.00,  150.00,  -15,  350)
) AS seed (policy_id, customer_id, policy_type, status, coverage_limit, deductible, start_day, end_day)
ON CONFLICT (policy_id) DO UPDATE SET
    customer_id    = EXCLUDED.customer_id,
    policy_type    = EXCLUDED.policy_type,
    status         = EXCLUDED.status,
    coverage_limit = EXCLUDED.coverage_limit,
    deductible     = EXCLUDED.deductible,
    start_date     = EXCLUDED.start_date,
    end_date       = EXCLUDED.end_date;
 