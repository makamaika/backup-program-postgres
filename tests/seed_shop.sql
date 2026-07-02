-- テストデータ: 多様なデータ型を含む
CREATE SCHEMA sales;

CREATE TABLE public.customers (
    id serial PRIMARY KEY,
    name text NOT NULL,
    email varchar(255) UNIQUE,
    age integer,
    balance numeric(12,2) DEFAULT 0,
    is_active boolean DEFAULT true,
    tags text[],
    profile jsonb,
    avatar bytea,
    joined_on date,
    created_at timestamptz DEFAULT now()
);
COMMENT ON TABLE public.customers IS '顧客マスタ';
COMMENT ON COLUMN public.customers.profile IS 'プロフィール(JSON)';

CREATE TABLE sales.orders (
    id bigserial PRIMARY KEY,
    customer_id integer REFERENCES public.customers(id),
    order_uuid uuid DEFAULT gen_random_uuid(),
    amount numeric(12,2) NOT NULL,
    status text CHECK (status IN ('pending','paid','shipped')),
    ordered_at timestamptz DEFAULT now()
);
CREATE INDEX idx_orders_customer ON sales.orders(customer_id);

CREATE VIEW public.active_customers AS
    SELECT id, name, email FROM public.customers WHERE is_active;

INSERT INTO public.customers (name, email, age, balance, is_active, tags, profile, avatar, joined_on)
SELECT
    '顧客' || i,
    'user' || i || '@example.com',
    20 + (i % 50),
    (random() * 100000)::numeric(12,2),
    i % 7 <> 0,
    ARRAY['tag' || (i % 5), 'vip'],
    jsonb_build_object('pref', '東京都', 'rank', i % 10),
    decode(md5(i::text), 'hex'),
    date '2020-01-01' + (i % 1500)
FROM generate_series(1, 500) AS i;

INSERT INTO sales.orders (customer_id, amount, status)
SELECT (i % 500) + 1, (random() * 50000)::numeric(12,2),
       (ARRAY['pending','paid','shipped'])[1 + i % 3]
FROM generate_series(1, 2000) AS i;
