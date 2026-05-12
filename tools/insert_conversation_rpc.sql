-- MCO: insert_conversation_event RPC
-- Run this in Supabase SQL Editor (same project).
-- Always returns exactly 1 JSONB row — avoids n8n empty-array problem
-- and provides reliable dedup detection (was_new: true/false).

CREATE OR REPLACE FUNCTION insert_conversation_event(
  p_event_id              UUID,
  p_lead_email            TEXT,
  p_timestamp             TIMESTAMPTZ,
  p_channel               TEXT,
  p_direction             TEXT,
  p_content               TEXT DEFAULT NULL,
  p_content_type          TEXT DEFAULT 'message',
  p_sender_name           TEXT DEFAULT NULL,
  p_intent                TEXT DEFAULT 'unknown',
  p_metadata              JSONB DEFAULT '{}',
  p_workflow_execution_id TEXT DEFAULT NULL
) RETURNS JSONB AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM conversations WHERE event_id = p_event_id) THEN
    RETURN jsonb_build_object('was_new', false, 'event_id', p_event_id::text);
  END IF;

  INSERT INTO conversations (
    event_id, lead_email, timestamp, channel, direction,
    content, content_type, sender_name, intent,
    metadata_json, workflow_execution_id
  ) VALUES (
    p_event_id, p_lead_email, p_timestamp, p_channel, p_direction,
    p_content, p_content_type, p_sender_name, p_intent,
    p_metadata, p_workflow_execution_id
  );

  RETURN jsonb_build_object('was_new', true, 'event_id', p_event_id::text);
END;
$$ LANGUAGE plpgsql;
