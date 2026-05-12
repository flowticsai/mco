-- MCO: fetch_lead_context RPC
-- Run this in Supabase SQL Editor (same project as supabase_setup.sql).
-- This function always returns exactly 1 JSONB item regardless of whether
-- data exists, which avoids the n8n empty-array problem where [] stops
-- all downstream nodes from running.

CREATE OR REPLACE FUNCTION fetch_lead_context(
  p_lead_email TEXT,
  p_max_events INT DEFAULT 20
) RETURNS JSONB AS $$
DECLARE
  v_lead          JSONB;
  v_conversations JSONB;
BEGIN
  SELECT to_jsonb(l.*) INTO v_lead
  FROM leads l
  WHERE l.lead_email = p_lead_email;

  SELECT jsonb_agg(c_ordered.*)
  INTO   v_conversations
  FROM (
    SELECT *
    FROM   conversations
    WHERE  lead_email = p_lead_email
    ORDER  BY timestamp DESC
    LIMIT  p_max_events
  ) c_ordered;

  RETURN jsonb_build_object(
    'lead',          v_lead,
    'conversations', COALESCE(v_conversations, '[]'::jsonb)
  );
END;
$$ LANGUAGE plpgsql STABLE;
