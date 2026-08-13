-- Full initial schema for the Telegram website-builder bot.
-- Mirrors alembic/versions/0001_initial_schema.py — paste into the Supabase SQL editor,
-- or once the project exists, tell Claude the project id and it can be applied via
-- the Supabase MCP `apply_migration` tool instead.

CREATE TABLE site_versions (
  id                uuid PRIMARY KEY,
  business_id       uuid NOT NULL,
  version_number    int NOT NULL,
  trigger           text NOT NULL,             -- 'create' | 'edit'
  code_storage_path text,
  sandbox_status    text NOT NULL DEFAULT 'pending',  -- 'pending' | 'passed' | 'failed'
  sandbox_report    jsonb,
  deployed_url      text,
  status            text NOT NULL DEFAULT 'pending',  -- 'pending' | 'generating' | 'testing' | 'deploying' | 'live' | 'failed'
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE businesses (
  id                  uuid PRIMARY KEY,
  owner_telegram_id   bigint NOT NULL,          -- not unique: one owner, many businesses
  slug                text NOT NULL UNIQUE,
  name                text NOT NULL,
  category            text NOT NULL,
  tagline             text,
  about               text,
  theme               text NOT NULL DEFAULT 'classic',   -- 'classic' | 'modern' | 'bold'
  phone               text,
  email               text,
  address             text,
  hours               jsonb NOT NULL DEFAULT '{}',
  social_links        jsonb NOT NULL DEFAULT '{}',
  status              text NOT NULL DEFAULT 'active',     -- 'active' | 'draft' | 'suspended'
  generation_status   text NOT NULL DEFAULT 'none',       -- 'none' | 'queued' | 'generating' | 'testing' | 'deploying' | 'live' | 'failed'
  current_version_id  uuid REFERENCES site_versions(id),
  deployment_url      text,
  vercel_project_id   text,
  plan                text NOT NULL DEFAULT 'free',       -- reserved for future billing phase
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_businesses_owner ON businesses(owner_telegram_id);

ALTER TABLE site_versions
  ADD CONSTRAINT fk_site_versions_business
  FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE;
CREATE INDEX idx_versions_business ON site_versions(business_id);

CREATE TABLE services (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name         text NOT NULL,
  description  text,
  price_label  text,             -- free-form: "$25", "from $50", "Contact for quote"
  sort_order   int NOT NULL DEFAULT 0,
  is_active    boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_services_business ON services(business_id);

CREATE TABLE media (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('logo','photo')),
  storage_path text NOT NULL,
  url          text NOT NULL,
  sort_order   int NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_media_business ON media(business_id);

CREATE TABLE edit_log (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id           uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  telegram_user_id      bigint NOT NULL,
  raw_message           text NOT NULL,
  parsed_operation      jsonb,
  applied               boolean NOT NULL DEFAULT false,
  triggered_version_id  uuid REFERENCES site_versions(id),
  error                 text,
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_edit_log_business ON edit_log(business_id);

-- Storage: create a public-read bucket named `business-media` via the Supabase
-- dashboard (Storage -> New bucket -> Public bucket) for logos/photos.
