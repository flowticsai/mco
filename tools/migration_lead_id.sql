-- MCO Lead Identity Migration
-- Replaces lead_email as universal PK with a UUID lead_id.
-- Multi-identifier resolution (email OR linkedin_url OR linkedin_urn OR phone) moves into upsert_lead().
-- Run this entire script in the Supabase SQL Editor.
-- Safe to re-run: all operations use IF NOT EXISTS / OR REPLACE / IF EXISTS.

-- ============================================================
-- PHASE 1: Add lead_id column to leads + new fields
-- ============================================================

ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_id UUID DEFAULT gen_random_uuid();

-- Backfill any rows that slipped through (shouldn't happen, but defensive)
UPDATE leads SET lead_id = gen_random_uuid() WHERE lead_id IS NULL;

ALTER TABLE leads ALTER COLUMN lead_id SET NOT NULL;

-- Store the latest LinkedIn conversation URN on the lead for quick Coordinator lookup
ALTER TABLE leads ADD COLUMN IF NOT EXISTS linkedin_conversation_urn TEXT;


-- ============================================================
-- PHASE 2: Add lead_id to dependent tables + backfill
-- ============================================================

ALTER TABLE conversations    ADD COLUMN IF NOT EXISTS lead_id UUID;
ALTER TABLE follow_up_queue  ADD COLUMN IF NOT EXISTS lead_id UUID;
ALTER TABLE phone_map        ADD COLUMN IF NOT EXISTS lead_id UUID;

UPDATE conversations c
  SET lead_id = l.lead_id
  FROM leads l
  WHERE c.lead_email = l.lead_email
    AND c.lead_id IS NULL;

UPDATE follow_up_queue f
  SET lead_id = l.lead_id
  FROM leads l
  WHERE f.lead_email = l.lead_email
    AND f.lead_id IS NULL;

UPDATE phone_map p
  SET lead_id = l.lead_id
  FROM leads l
  WHERE p.lead_email = l.lead_email
    AND p.lead_id IS NULL;


-- ============================================================
-- PHASE 3: Drop old FK constraints (reference leads.lead_email)
-- Must happen before lead_email loses its PK status.
-- ============================================================

ALTER TABLE conversations    DROP CONSTRAINT IF EXISTS conversations_lead_email_fkey;
ALTER TABLE follow_up_queue  DROP CONSTRAINT IF EXISTS follow_up_queue_lead_email_fkey;
ALTER TABLE phone_map        DROP CONSTRAINT IF EXISTS phone_map_lead_email_fkey;


-- ============================================================
-- PHASE 4: Make lead_id NOT NULL in dependent tables
-- All rows should be backfilled; any null means orphaned data.
-- ============================================================

ALTER TABLE conversations    ALTER COLUMN lead_id SET NOT NULL;
ALTER TABLE follow_up_queue  ALTER COLUMN lead_id SET NOT NULL;
ALTER TABLE phone_map        ALTER COLUMN lead_id SET NOT NULL;


-- ============================================================
-- PHASE 5: Swap PK on leads from lead_email → lead_id
-- ============================================================

ALTER TABLE leads DROP CONSTRAINT leads_pkey;
ALTER TABLE leads ALTER COLUMN lead_email DROP NOT NULL;
ALTER TABLE leads ADD PRIMARY KEY (lead_id);


-- ============================================================
-- PHASE 6: Unique indexes — partial (allow NULL, enforce uniqueness when set)
-- PostgreSQL FK constraints cannot reference partial indexes, but all
-- FKs in dependent tables now point to lead_id, not lead_email, so
-- partial indexes here are safe for lookup purposes.
-- ============================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique
  ON leads(lead_email) WHERE lead_email IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_url_unique
  ON leads(linkedin_profile_url) WHERE linkedin_profile_url IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_linkedin_urn_unique
  ON leads(linkedin_urn) WHERE linkedin_urn IS NOT NULL;

-- phone_e164 already has an index from supabase_setup.sql; add unique variant
DROP INDEX IF EXISTS idx_leads_phone;
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_phone_unique
  ON leads(phone_e164) WHERE phone_e164 IS NOT NULL;


-- ============================================================
-- PHASE 7: Add new FK constraints on lead_id
-- ============================================================

ALTER TABLE conversations
  ADD CONSTRAINT conversations_lead_id_fkey
  FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
  ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE follow_up_queue
  ADD CONSTRAINT follow_up_queue_lead_id_fkey
  FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
  ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE phone_map
  ADD CONSTRAINT phone_map_lead_id_fkey
  FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
  ON UPDATE CASCADE ON DELETE CASCADE;


-- ============================================================
-- PHASE 8: New indexes for lead_id columns
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_conv_lead_id     ON conversations(lead_id);
CREATE INDEX IF NOT EXISTS idx_queue_lead_id    ON follow_up_queue(lead_id);
CREATE INDEX IF NOT EXISTS idx_phonemap_lead_id ON phone_map(lead_id);

-- Keep old lead_email indexes on dependent tables for backwards-compat reads
-- (existing rows still have lead_email populated; new LinkedIn-only rows won't)
-- idx_conv_lead_time, idx_queue_lead remain useful


-- ============================================================
-- PHASE 9: Rewrite upsert_lead()
-- Now accepts any combination of identifiers. Resolves existing
-- lead by email OR linkedin_profile_url OR linkedin_urn OR phone.
-- Returns lead_id so callers can thread it through.
-- ============================================================

CREATE OR REPLACE FUNCTION upsert_lead(
  p_lead_email                TEXT    DEFAULT NULL,
  p_linkedin_profile_url      TEXT    DEFAULT NULL,
  p_linkedin_urn              TEXT    DEFAULT NULL,
  p_phone_e164                TEXT    DEFAULT NULL,
  p_full_name                 TEXT    DEFAULT NULL,
  p_company                   TEXT    DEFAULT NULL,
  p_channel                   TEXT    DEFAULT NULL,
  p_monday_item_id            TEXT    DEFAULT NULL,
  p_new_intent                TEXT    DEFAULT NULL,
  p_aimfox_campaign           TEXT    DEFAULT NULL,
  p_email_sender_inbox        TEXT    DEFAULT NULL,
  p_linkedin_conversation_urn TEXT    DEFAULT NULL
) RETURNS TABLE(
  lead_id        UUID,
  lead_email     TEXT,
  lead_created   BOOLEAN,
  monday_item_id TEXT,
  overall_intent TEXT
) AS $$
DECLARE
  v_lead_id        UUID;
  v_lead_email     TEXT;
  v_created        BOOLEAN := FALSE;
  v_monday_item_id TEXT;
  v_overall_intent TEXT;
BEGIN
  -- Multi-identifier resolution: first existing lead that matches any supplied identifier.
  -- ORDER BY created_at ASC picks the oldest record when multiple identifiers happen
  -- to map to different rows (data anomaly); oldest row is canonical.
  SELECT l.lead_id, l.lead_email
    INTO v_lead_id, v_lead_email
    FROM leads l
    WHERE (p_lead_email          IS NOT NULL AND l.lead_email           = p_lead_email)
       OR (p_linkedin_profile_url IS NOT NULL AND l.linkedin_profile_url = p_linkedin_profile_url)
       OR (p_linkedin_urn         IS NOT NULL AND l.linkedin_urn          = p_linkedin_urn)
       OR (p_phone_e164           IS NOT NULL AND l.phone_e164            = p_phone_e164)
    ORDER BY l.created_at ASC
    LIMIT 1;

  IF v_lead_id IS NULL THEN
    -- New lead
    v_created := TRUE;
    INSERT INTO leads (
      lead_email, full_name, company,
      linkedin_urn, linkedin_profile_url, linkedin_conversation_urn,
      phone_e164, first_channel, first_seen_at,
      last_activity_at, last_active_channel,
      monday_item_id, overall_intent, overall_intent_updated_at,
      aimfox_campaign, email_sender_inbox, created_at
    ) VALUES (
      p_lead_email,
      COALESCE(p_full_name, ''),
      COALESCE(p_company, ''),
      p_linkedin_urn,
      p_linkedin_profile_url,
      p_linkedin_conversation_urn,
      p_phone_e164,
      p_channel,
      NOW(), NOW(), p_channel,
      p_monday_item_id,
      COALESCE(p_new_intent, 'unknown'),
      NOW(),
      p_aimfox_campaign,
      p_email_sender_inbox,
      NOW()
    )
    RETURNING leads.lead_id, leads.lead_email INTO v_lead_id, v_lead_email;

  ELSE
    -- Existing lead: fill in any missing identifiers, promote intent
    UPDATE leads SET
      lead_email           = COALESCE(p_lead_email,           leads.lead_email),
      full_name            = COALESCE(NULLIF(p_full_name, ''),  leads.full_name),
      company              = COALESCE(NULLIF(p_company, ''),    leads.company),
      linkedin_urn         = COALESCE(p_linkedin_urn,          leads.linkedin_urn),
      linkedin_profile_url = COALESCE(p_linkedin_profile_url,  leads.linkedin_profile_url),
      linkedin_conversation_urn = COALESCE(p_linkedin_conversation_urn, leads.linkedin_conversation_urn),
      phone_e164           = COALESCE(p_phone_e164,            leads.phone_e164),
      last_activity_at     = NOW(),
      last_active_channel  = COALESCE(p_channel,               leads.last_active_channel),
      monday_item_id       = COALESCE(p_monday_item_id,        leads.monday_item_id),
      aimfox_campaign      = COALESCE(p_aimfox_campaign,       leads.aimfox_campaign),
      email_sender_inbox   = COALESCE(p_email_sender_inbox,    leads.email_sender_inbox),
      overall_intent       = CASE
        WHEN intent_rank(COALESCE(p_new_intent, 'unknown')) > intent_rank(leads.overall_intent)
        THEN COALESCE(p_new_intent, 'unknown')
        ELSE leads.overall_intent
      END,
      overall_intent_updated_at = CASE
        WHEN intent_rank(COALESCE(p_new_intent, 'unknown')) > intent_rank(leads.overall_intent)
        THEN NOW()
        ELSE leads.overall_intent_updated_at
      END
    WHERE leads.lead_id = v_lead_id
    RETURNING leads.lead_email INTO v_lead_email;
  END IF;

  SELECT l.monday_item_id, l.overall_intent
    INTO v_monday_item_id, v_overall_intent
    FROM leads l WHERE l.lead_id = v_lead_id;

  RETURN QUERY SELECT v_lead_id, v_lead_email, v_created, v_monday_item_id, v_overall_intent;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- PHASE 10: Rewrite insert_conversation_event()
-- Accepts p_lead_id (preferred) or p_lead_email (fallback).
-- Returns lead_id in the response JSON.
-- ============================================================

CREATE OR REPLACE FUNCTION insert_conversation_event(
  p_event_id              UUID,
  p_lead_id               UUID        DEFAULT NULL,
  p_lead_email            TEXT        DEFAULT NULL,
  p_timestamp             TIMESTAMPTZ DEFAULT NOW(),
  p_channel               TEXT        DEFAULT NULL,
  p_direction             TEXT        DEFAULT NULL,
  p_content               TEXT        DEFAULT NULL,
  p_content_type          TEXT        DEFAULT 'message',
  p_sender_name           TEXT        DEFAULT NULL,
  p_intent                TEXT        DEFAULT 'unknown',
  p_metadata              JSONB       DEFAULT '{}',
  p_workflow_execution_id TEXT        DEFAULT NULL
) RETURNS JSONB AS $$
DECLARE
  v_lead_id    UUID;
  v_lead_email TEXT;
BEGIN
  -- Dedup: silent ignore if event already recorded
  IF EXISTS (SELECT 1 FROM conversations WHERE event_id = p_event_id) THEN
    RETURN jsonb_build_object('was_new', false, 'event_id', p_event_id::text);
  END IF;

  -- Resolve lead
  IF p_lead_id IS NOT NULL THEN
    v_lead_id := p_lead_id;
    SELECT lead_email INTO v_lead_email FROM leads WHERE lead_id = v_lead_id;
  ELSIF p_lead_email IS NOT NULL THEN
    SELECT lead_id, lead_email INTO v_lead_id, v_lead_email
      FROM leads WHERE lead_email = p_lead_email;
  END IF;

  IF v_lead_id IS NULL THEN
    RETURN jsonb_build_object(
      'was_new', false,
      'error',   'lead_not_found',
      'event_id', p_event_id::text
    );
  END IF;

  INSERT INTO conversations (
    event_id, lead_id, lead_email, timestamp, channel, direction,
    content, content_type, sender_name, intent,
    metadata_json, workflow_execution_id
  ) VALUES (
    p_event_id, v_lead_id, v_lead_email,
    p_timestamp, p_channel, p_direction,
    p_content, p_content_type, p_sender_name, p_intent,
    p_metadata, p_workflow_execution_id
  );

  RETURN jsonb_build_object(
    'was_new',  true,
    'event_id', p_event_id::text,
    'lead_id',  v_lead_id::text
  );
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- PHASE 11: Rewrite fetch_lead_context()
-- Accepts lead_id, lead_email, linkedin_profile_url, or phone.
-- Queries conversations by lead_id (fast indexed lookup).
-- ============================================================

CREATE OR REPLACE FUNCTION fetch_lead_context(
  p_lead_id              UUID    DEFAULT NULL,
  p_lead_email           TEXT    DEFAULT NULL,
  p_linkedin_profile_url TEXT    DEFAULT NULL,
  p_phone_e164           TEXT    DEFAULT NULL,
  p_max_events           INT     DEFAULT 20
) RETURNS JSONB AS $$
DECLARE
  v_lead_id       UUID;
  v_lead          JSONB;
  v_conversations JSONB;
BEGIN
  -- Resolve lead_id from whichever identifier was supplied
  IF p_lead_id IS NOT NULL THEN
    v_lead_id := p_lead_id;
  ELSIF p_lead_email IS NOT NULL THEN
    SELECT lead_id INTO v_lead_id FROM leads WHERE lead_email = p_lead_email;
  ELSIF p_linkedin_profile_url IS NOT NULL THEN
    SELECT lead_id INTO v_lead_id FROM leads WHERE linkedin_profile_url = p_linkedin_profile_url;
  ELSIF p_phone_e164 IS NOT NULL THEN
    SELECT lead_id INTO v_lead_id FROM leads WHERE phone_e164 = p_phone_e164;
  END IF;

  IF v_lead_id IS NULL THEN
    RETURN jsonb_build_object(
      'lead',          NULL,
      'conversations', '[]'::jsonb,
      'error',         'lead_not_found'
    );
  END IF;

  SELECT to_jsonb(l.*) INTO v_lead FROM leads l WHERE l.lead_id = v_lead_id;

  SELECT jsonb_agg(c_ordered.*)
    INTO v_conversations
    FROM (
      SELECT *
      FROM   conversations
      WHERE  lead_id = v_lead_id
      ORDER  BY timestamp DESC
      LIMIT  p_max_events
    ) c_ordered;

  RETURN jsonb_build_object(
    'lead',          v_lead,
    'conversations', COALESCE(v_conversations, '[]'::jsonb)
  );
END;
$$ LANGUAGE plpgsql STABLE;


-- ============================================================
-- PHASE 12: Add outcome column to follow_up_queue (added 2026-05-18)
-- Records the result of a voice call after Post Call Analysis runs.
-- Values: answered | voicemail | no_answer | failed | NULL (not yet called)
-- status stays as sent/pending — outcome is a separate concern.
-- ============================================================

ALTER TABLE follow_up_queue ADD COLUMN IF NOT EXISTS outcome TEXT;


-- ============================================================
-- VERIFY
-- ============================================================
SELECT
  (SELECT COUNT(*) FROM leads)           AS leads_total,
  (SELECT COUNT(*) FROM leads WHERE lead_id IS NOT NULL) AS leads_with_id,
  (SELECT COUNT(*) FROM conversations WHERE lead_id IS NOT NULL) AS convs_with_id,
  (SELECT COUNT(*) FROM follow_up_queue WHERE lead_id IS NOT NULL) AS queue_with_id;
