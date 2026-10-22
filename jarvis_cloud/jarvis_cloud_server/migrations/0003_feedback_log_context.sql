-- Debugging context for a feedback report, since a report often says
-- little more than "not working": the client's own timestamp for when Send was
-- clicked, and a recent excerpt of the local daemon log leading up to
-- it. Both nullable - existing rows, and any client that hasn't picked
-- up this change yet, have neither.
ALTER TABLE feedback ADD COLUMN client_timestamp TIMESTAMPTZ;
ALTER TABLE feedback ADD COLUMN log_excerpt TEXT;
