-- İzmir Open Data MCP: ilk şema
-- Supabase SQL Editor'de çalıştır ya da: psql "$DATABASE_URL" -f supabase/migrations/0001_init.sql
-- Tekrar çalıştırılabilir (idempotent).

create extension if not exists pg_trgm;
create extension if not exists pgcrypto;

-- 30 ilçe ve merkez koordinatı (muhtarlıkların medyanı)
create table if not exists districts (
    id                  integer primary key,          -- belediyenin ILCEID değeri
    name                text not null unique,
    name_norm           text not null,
    lat                 double precision not null,
    lon                 double precision not null,
    neighborhood_count  integer not null default 0,
    updated_at          timestamptz not null default now()
);

-- ESHOT otobüs durakları
create table if not exists stops (
    id           text primary key,                    -- ESHOT durak kodu, ör. '40010'
    name         text not null,
    name_norm    text not null,
    lat          double precision,
    lon          double precision,
    district_id  integer references districts(id),
    lines        text[] not null default '{}',        -- duraktan geçen hat numaraları
    updated_at   timestamptz not null default now()
);
create index if not exists stops_name_trgm on stops using gin (name_norm gin_trgm_ops);
create index if not exists stops_district on stops (district_id);

-- ESHOT hatları
create table if not exists bus_lines (
    number      text primary key,                     -- ör. '505'
    name        text not null,
    start_name  text,
    end_name    text,
    updated_at  timestamptz not null default now()
);

-- Metro ve İZBAN istasyonları
create table if not exists stations (
    id          bigint generated always as identity primary key,
    system      text not null check (system in ('metro', 'izban', 'tram')),
    source_id   integer not null,                     -- kaynaktaki IstasyonId
    name        text not null,
    name_norm   text not null,
    lat         double precision not null,
    lon         double precision not null,
    seq         integer,
    active      boolean not null default true,
    updated_at  timestamptz not null default now(),
    unique (system, source_id)
);
create index if not exists stations_name_trgm on stations using gin (name_norm gin_trgm_ops);

-- İZDENİZ iskeleleri
create table if not exists piers (
    id          integer primary key,                  -- IskeleId
    name        text not null,
    name_norm   text not null,
    lat         double precision not null,
    lon         double precision not null,
    car_ferry   boolean not null default false,
    active      boolean not null default true,
    updated_at  timestamptz not null default now()
);

-- Tüm eczaneler (nöbetçi listesi canlı API'den gelir, burada tutulmaz)
create table if not exists pharmacies (
    id           integer primary key,                 -- EczaneId
    name         text not null,
    address      text,
    phone        text,
    region       text,                                -- eczacı odası nöbet bölgesi, ör. 'BUCA 1'
    district_id  integer references districts(id),
    lat          double precision,
    lon          double precision,
    updated_at   timestamptz not null default now()
);
create index if not exists pharmacies_district on pharmacies (district_id);

-- Uzak sunucu için API anahtarları (3. hafta). Anahtarın kendisi değil, SHA-256 özeti saklanır.
create table if not exists api_keys (
    id           uuid primary key default gen_random_uuid(),
    key_hash     text not null unique,
    key_prefix   text not null,                       -- tanımak için ilk 8 karakter, ör. 'izm_3f9a'
    owner        text not null,
    daily_quota  integer not null default 1000,
    created_at   timestamptz not null default now(),
    revoked_at   timestamptz
);

-- Araç çağrı kayıtları: değerlendirme ve maliyet raporu için
create table if not exists usage_logs (
    id          bigint generated always as identity primary key,
    api_key_id  uuid references api_keys(id) on delete set null,
    tool        text not null,
    ok          boolean not null,
    error_code  text,
    latency_ms  integer,
    cached      boolean not null default false,
    created_at  timestamptz not null default now()
);
create index if not exists usage_logs_created on usage_logs (created_at desc);
create index if not exists usage_logs_key_day on usage_logs (api_key_id, created_at);

-- Supabase tabloları anon anahtarla REST üzerinden dışarı açar. RLS açık ve politika yok =
-- dışarıdan kimse okuyamaz/yazamaz; sunucumuz doğrudan Postgres bağlantısıyla (postgres rolü) erişir.
alter table districts  enable row level security;
alter table stops      enable row level security;
alter table bus_lines  enable row level security;
alter table stations   enable row level security;
alter table piers      enable row level security;
alter table pharmacies enable row level security;
alter table api_keys   enable row level security;
alter table usage_logs enable row level security;
