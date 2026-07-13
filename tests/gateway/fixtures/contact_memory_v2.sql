-- Historical contact-memory schema v2, copied from the released v2 schema.
-- Representative rows exercise every mutable/historical ledger that v3 must preserve.
PRAGMA foreign_keys=ON;
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '2');
INSERT INTO schema_meta VALUES ('historical_fixture_marker', 'preserve-me');

CREATE TABLE fact (
  version_id TEXT PRIMARY KEY,
  logical_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_text TEXT NOT NULL,
  audience TEXT NOT NULL CHECK(audience IN ('owner_only','owner_review','guest_ok','public')),
  mention_policy TEXT NOT NULL CHECK(mention_policy IN ('background','mentionable','sensitive','restricted')),
  assertion_type TEXT NOT NULL CHECK(assertion_type IN ('stated','observed','inferred')),
  source_id TEXT NOT NULL,
  source_contact_id TEXT NOT NULL,
  evidence_pointer TEXT NOT NULL DEFAULT '',
  trust REAL NOT NULL CHECK(trust >= 0 AND trust <= 1),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  status TEXT NOT NULL CHECK(status IN ('active','pending','quarantined','superseded','withdrawn','rejected')),
  valid_from REAL NOT NULL,
  valid_to REAL,
  tx_from REAL NOT NULL,
  tx_to REAL,
  created_at REAL NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  CHECK(valid_to IS NULL OR valid_to > valid_from),
  CHECK(tx_to IS NULL OR tx_to >= tx_from)
);
CREATE UNIQUE INDEX one_active_fact
  ON fact(logical_id) WHERE status='active' AND tx_to IS NULL;
CREATE INDEX fact_visibility
  ON fact(status, audience, mention_policy, assertion_type, trust, confidence);
INSERT INTO fact VALUES (
  'fact-v2-1','vehicle:color','person:contact','vehicle_color','The car is silver.',
  'owner_only','background','stated','message:v2','contact','message:v2',
  0.97,0.98,'active',100.0,NULL,100.0,NULL,100.0,'{"fixture":"v2"}'
);

CREATE TABLE embedding (
  version_id TEXT NOT NULL REFERENCES fact(version_id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dimensions INTEGER NOT NULL CHECK(dimensions > 0),
  vector_le_f32 BLOB NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(version_id, model_id)
);

CREATE TABLE edge (
  edge_version_id TEXT PRIMARY KEY,
  logical_id TEXT NOT NULL,
  source_entity_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  target_entity_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  mention_policy TEXT NOT NULL,
  assertion_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_contact_id TEXT NOT NULL,
  evidence_pointer TEXT NOT NULL DEFAULT '',
  trust REAL NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL,
  valid_from REAL NOT NULL,
  valid_to REAL,
  tx_from REAL NOT NULL,
  tx_to REAL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX one_active_edge
  ON edge(logical_id) WHERE status='active' AND tx_to IS NULL;

CREATE TABLE pending_fact (
  proposal_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_contact_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','superseded','promoted')),
  created_at REAL NOT NULL,
  decided_at REAL
);
INSERT INTO pending_fact VALUES (
  'pending-v2-1','idem-v2-1',
  '{"logical_id":"food:favorite","subject_id":"person:contact","predicate":"likes","object_text":"Likes ramen.","source_id":"message:pending-v2","source_contact_id":"contact","audience":"owner_review","mention_policy":"background","assertion_type":"stated","trust":0.8,"confidence":0.8,"status":"pending","evidence_pointer":"message:pending-v2","valid_from":null,"valid_to":null,"metadata":{}}',
  'message:pending-v2','contact','pending',110.0,NULL
);

CREATE TABLE recommendation (
  recommendation_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  basis_fact_ids_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','active','withdrawn','fulfilled','rejected')),
  supersedes_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL,
  expires_at REAL,
  change_requirements_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT UNIQUE
);
CREATE UNIQUE INDEX one_active_recommendation
  ON recommendation(topic) WHERE status='active';
INSERT INTO recommendation VALUES (
  'recommendation-v2-1','vehicle','Keep the silver car.','["fact-v2-1"]',
  0.91,'active',NULL,120.0,121.0,9999.0,'["color changes"]','recommendation-idem-v2'
);

CREATE TABLE recall_event (
  event_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  fact_version_id TEXT NOT NULL REFERENCES fact(version_id),
  event_type TEXT NOT NULL CHECK(event_type IN ('retrieved','used')),
  created_at REAL NOT NULL,
  UNIQUE(session_key, turn_index, fact_version_id, event_type)
);
CREATE INDEX recall_cooldown
  ON recall_event(session_key, event_type, created_at, turn_index);
INSERT INTO recall_event VALUES ('recall-v2-1','session-v2',7,'fact-v2-1','retrieved',130.0);

CREATE TABLE callback_event (
  event_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN ('fact','recommendation')),
  subject_id TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(session_key, subject_type, subject_id, turn_index)
);
CREATE INDEX callback_event_cooldown
  ON callback_event(session_key, subject_type, subject_id, created_at, turn_index);
INSERT INTO callback_event VALUES ('callback-v2-1','session-v2','fact','fact-v2-1',8,140.0);
