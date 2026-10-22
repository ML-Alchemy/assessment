-- Relay's customers, matching fixtures/customers.yaml.
-- CRM tokens are filled in when each customer connects their CRM (make seed).

INSERT INTO customers (id, name, allowed_llm_providers) VALUES
    ('cust_fernway', 'Fernway Software', ARRAY['provider-a', 'provider-b']),
    ('cust_halden',  'Halden Analytics', ARRAY['provider-a']),
    ('cust_quarry',  'Quarry Labs',      ARRAY['provider-b']);
