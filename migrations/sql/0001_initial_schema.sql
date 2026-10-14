-- migrations/sql/0001_initial_schema.sql
-- RIC POC schema v1.2: 22 tables in six groups. Applied by revision 0001.
-- Statements are run one at a time; do not put two statements on one line.

-- =====================================================================
-- Shared objects
-- =====================================================================
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- Group 1: Master data
-- =====================================================================
CREATE TABLE stores (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_code          text NOT NULL UNIQUE CHECK (site_code ~ '^[0-9]{5}$'),
    name               text NOT NULL,
    region             text NOT NULL,
    country_code       char(2) NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    city               text,
    address_line       text,
    postal_code        text,
    latitude           numeric(9,6) CHECK (latitude BETWEEN -90 AND 90),
    longitude          numeric(9,6) CHECK (longitude BETWEEN -180 AND 180),
    timezone           text NOT NULL,
    business_hours     jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(business_hours) = 'object'),
    contact_phone      text,
    contact_email      text,
    status             text NOT NULL DEFAULT 'open'
                       CHECK (status IN ('planned', 'open', 'temporarily_closed', 'closed')),
    servicenow_sys_id  text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((latitude IS NULL) = (longitude IS NULL))
);
CREATE INDEX ix_stores_region_country ON stores (region, country_code);
CREATE TRIGGER trg_stores_updated BEFORE UPDATE ON stores
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE assignment_groups (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text NOT NULL UNIQUE,
    description        text,
    email              text,
    servicenow_sys_id  text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_groups_updated BEFORE UPDATE ON assignment_groups
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE network_element_types (
    code                  text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]*$'),
    display_name          text NOT NULL,
    business_criticality  text NOT NULL CHECK (business_criticality IN ('high', 'medium', 'low')),
    aruba_device_type     text CHECK (aruba_device_type IN ('ACCESS POINT', 'SWITCH', 'GATEWAY')),
    servicenow_ci_class   text,
    sort_order            smallint NOT NULL DEFAULT 0,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_net_types_updated BEFORE UPDATE ON network_element_types
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE device_types (
    code                  text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]*$'),
    display_name          text NOT NULL,
    business_criticality  text NOT NULL CHECK (business_criticality IN ('high', 'medium', 'low')),
    servicenow_ci_class   text,
    sort_order            smallint NOT NULL DEFAULT 0,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_device_types_updated BEFORE UPDATE ON device_types
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Group 2: Network and device inventory
-- =====================================================================
CREATE TABLE network_elements (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            uuid NOT NULL REFERENCES stores(id),
    element_type_code   text NOT NULL REFERENCES network_element_types(code),
    name                text NOT NULL,
    role                text,
    location_in_store   text,
    serial_number       text UNIQUE,
    mac_address         macaddr,
    ip_address          inet,
    manufacturer        text,
    model               text,
    uplink_element_id   uuid,
    uplink_port         text,
    source_gateway_id   uuid,
    source_uplink_name  text,
    is_active           boolean NOT NULL DEFAULT true,
    attributes          jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
    servicenow_sys_id   text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, name),
    UNIQUE (id, store_id),
    FOREIGN KEY (uplink_element_id, store_id) REFERENCES network_elements (id, store_id),
    FOREIGN KEY (source_gateway_id, store_id) REFERENCES network_elements (id, store_id),
    CHECK (uplink_element_id IS NULL OR uplink_element_id <> id),
    CHECK ((source_gateway_id IS NULL) = (source_uplink_name IS NULL)),
    CHECK ((element_type_code IN ('router', 'wan_link')) = (source_gateway_id IS NOT NULL)),
    CHECK (element_type_code IN ('router', 'wan_link') OR serial_number IS NOT NULL)
);
CREATE UNIQUE INDEX ux_ne_mac ON network_elements (mac_address) WHERE mac_address IS NOT NULL;
CREATE UNIQUE INDEX ux_ne_gateway_uplink
    ON network_elements (source_gateway_id, source_uplink_name, element_type_code)
    WHERE source_gateway_id IS NOT NULL;
CREATE INDEX ix_ne_store_type ON network_elements (store_id, element_type_code) WHERE is_active;
CREATE TRIGGER trg_network_elements_updated BEFORE UPDATE ON network_elements
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE devices (
    id                            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id                      uuid NOT NULL REFERENCES stores(id),
    device_type_code              text NOT NULL REFERENCES device_types(code),
    name                          text NOT NULL,
    location_in_store             text,
    serial_number                 text UNIQUE,
    mac_address                   macaddr,
    ip_address                    inet,
    manufacturer                  text,
    model                         text,
    connection_type               text CHECK (connection_type IN ('wired', 'wireless')),
    connected_network_element_id  uuid,
    connected_port                text,
    user_role                     text,
    vlan                          text,
    status                        text NOT NULL DEFAULT 'online'
                                  CHECK (status IN ('online', 'degraded', 'offline', 'unknown')),
    status_since                  timestamptz NOT NULL DEFAULT now(),
    status_reason                 text,
    last_heartbeat_at             timestamptz DEFAULT now(),
    is_active                     boolean NOT NULL DEFAULT true,
    attributes                    jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
    servicenow_sys_id             text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at                    timestamptz NOT NULL DEFAULT now(),
    updated_at                    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, name),
    UNIQUE (id, store_id),
    FOREIGN KEY (connected_network_element_id, store_id) REFERENCES network_elements (id, store_id),
    CHECK (connected_port IS NULL OR connected_network_element_id IS NOT NULL)
);
CREATE UNIQUE INDEX ux_devices_mac ON devices (mac_address) WHERE mac_address IS NOT NULL;
CREATE INDEX ix_devices_store_type ON devices (store_id, device_type_code) WHERE is_active;
CREATE INDEX ix_devices_connected ON devices (connected_network_element_id)
    WHERE connected_network_element_id IS NOT NULL;
CREATE TRIGGER trg_devices_updated BEFORE UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE raw_network_events (
    event_id           text PRIMARY KEY,
    schema_version     text NOT NULL,
    event_type         text NOT NULL,
    event_time         timestamptz NOT NULL,
    ingestion_time     timestamptz NOT NULL,
    source             text NOT NULL,
    vendor             text NOT NULL,
    device_id          text,
    site_id            text NOT NULL,
    country            text,
    incident_id        text,
    correlation_id     text,
    payload            jsonb NOT NULL,
    processing_status  text NOT NULL DEFAULT 'pending'
                       CHECK (processing_status IN ('pending', 'processed', 'duplicate', 'unmatched', 'error')),
    processed_at       timestamptz,
    process_error      text
);
CREATE INDEX ix_raw_pending ON raw_network_events (event_time) WHERE processing_status = 'pending';

CREATE TABLE network_events (
    event_id              text PRIMARY KEY REFERENCES raw_network_events(event_id),
    store_id              uuid NOT NULL REFERENCES stores(id),
    network_element_id    uuid,
    matched_by            text NOT NULL CHECK (matched_by IN ('serial', 'device_mac', 'gateway_uplink', 'none')),
    occurred_at           timestamptz NOT NULL,
    ingested_at           timestamptz NOT NULL,
    source                text NOT NULL,
    vendor_event_uuid     text UNIQUE,
    vendor_event_number   text,
    vendor_event_type     text NOT NULL,
    correlation_id        text,
    aruba_device_type     text,
    hostname              text,
    device_serial         text,
    device_mac            macaddr,
    level                 text,
    description           text,
    reported_status       text CHECK (reported_status IN ('online', 'offline')),
    status_applied        boolean NOT NULL DEFAULT false,
    uplink_name           text,
    client_mac            macaddr,
    client_ip             inet,
    client_hostname       text,
    username              text,
    user_role             text,
    vlan                  text,
    bssid                 text,
    ssid                  text,
    band                  text,
    channel               text,
    rssi                  smallint,
    snr                   smallint,
    retry_rate            smallint,
    reason                text,
    dhcp_latency_ms       integer,
    roaming_latency_ms    integer,
    details               jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details) = 'object'),
    FOREIGN KEY (network_element_id, store_id) REFERENCES network_elements (id, store_id),
    CHECK ((matched_by = 'none') = (network_element_id IS NULL))
);
CREATE INDEX ix_ne_events_element_time ON network_events (network_element_id, occurred_at DESC);
CREATE INDEX ix_ne_events_store_time ON network_events (store_id, occurred_at DESC);
CREATE INDEX ix_ne_events_status ON network_events (network_element_id, occurred_at DESC)
    WHERE reported_status IS NOT NULL;
CREATE INDEX ix_ne_events_client ON network_events (client_mac, occurred_at DESC)
    WHERE client_mac IS NOT NULL;

CREATE TABLE network_element_current_status (
    network_element_id    uuid PRIMARY KEY,
    store_id              uuid NOT NULL REFERENCES stores(id),
    status                text NOT NULL DEFAULT 'unknown'
                          CHECK (status IN ('online', 'offline', 'unknown')),
    status_since          timestamptz,
    last_status_event_id  text REFERENCES network_events(event_id),
    last_event_at         timestamptz,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (network_element_id, store_id) REFERENCES network_elements (id, store_id),
    CHECK ((status = 'unknown') OR (status_since IS NOT NULL AND last_status_event_id IS NOT NULL))
);
CREATE INDEX ix_necs_store_status ON network_element_current_status (store_id, status);
CREATE TRIGGER trg_necs_updated BEFORE UPDATE ON network_element_current_status
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Group 3: ITSM
-- =====================================================================
CREATE TABLE incidents (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number                      text NOT NULL UNIQUE CHECK (number ~ '^INC[0-9]{7,}$'),
    store_id                    uuid NOT NULL REFERENCES stores(id),
    short_description           text NOT NULL,
    description                 text,
    u_symptom                   text,
    business_service            text,
    category                    text NOT NULL CHECK (category IN ('network', 'hardware', 'software', 'inquiry')),
    subcategory                 text,
    cmdb_ci_network_element_id  uuid,
    cmdb_ci_device_id           uuid,
    contact_type                text NOT NULL CHECK (contact_type IN
                                  ('monitoring', 'phone', 'email', 'self_service', 'walk_in')),
    caller                      text,
    opened_by                   text,
    impact                      smallint NOT NULL CHECK (impact BETWEEN 1 AND 3),
    urgency                     smallint NOT NULL CHECK (urgency BETWEEN 1 AND 3),
    priority                    smallint GENERATED ALWAYS AS (impact + urgency - 1) STORED,
    state                       smallint NOT NULL DEFAULT 1 CHECK (state IN (1, 2, 3, 6, 7, 8)),
    hold_reason                 text CHECK (hold_reason IN ('awaiting_caller', 'awaiting_vendor',
                                  'awaiting_site_visit', 'awaiting_change', 'awaiting_problem')),
    assignment_group_id         uuid REFERENCES assignment_groups(id),
    assigned_to                 text,
    u_resolution_eta            timestamptz,
    u_eta_note                  text,
    opened_at                   timestamptz NOT NULL,
    resolved_at                 timestamptz,
    closed_at                   timestamptz,
    close_code                  text,
    close_notes                 text,
    servicenow_sys_id           text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, store_id),
    FOREIGN KEY (cmdb_ci_network_element_id, store_id) REFERENCES network_elements (id, store_id),
    FOREIGN KEY (cmdb_ci_device_id, store_id) REFERENCES devices (id, store_id),
    CHECK (num_nonnulls(cmdb_ci_network_element_id, cmdb_ci_device_id) <= 1),
    CHECK ((state = 3) = (hold_reason IS NOT NULL)),
    CHECK (state NOT IN (6, 7) OR resolved_at IS NOT NULL),
    CHECK (state <> 7 OR closed_at IS NOT NULL),
    CHECK (resolved_at IS NULL OR resolved_at >= opened_at)
);
CREATE INDEX ix_inc_store_active ON incidents (store_id, opened_at DESC) WHERE state IN (1, 2, 3);
CREATE INDEX ix_inc_portal ON incidents (state, priority, opened_at DESC);
CREATE INDEX ix_inc_group ON incidents (assignment_group_id, state);
CREATE INDEX ix_inc_ci_network ON incidents (cmdb_ci_network_element_id)
    WHERE cmdb_ci_network_element_id IS NOT NULL;
CREATE INDEX ix_inc_ci_device ON incidents (cmdb_ci_device_id) WHERE cmdb_ci_device_id IS NOT NULL;
CREATE TRIGGER trg_incidents_updated BEFORE UPDATE ON incidents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE incident_journal (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id        uuid NOT NULL REFERENCES incidents(id),
    element            text NOT NULL CHECK (element IN ('comments', 'work_notes', 'system')),
    value              text NOT NULL CHECK (length(trim(value)) > 0),
    author_type        text NOT NULL CHECK (author_type IN ('it_staff', 'store_user', 'system', 'agent')),
    created_by         text NOT NULL,
    source             text NOT NULL CHECK (source IN ('servicenow', 'admin_portal', 'mobile_app', 'agent', 'seed')),
    process_status     text NOT NULL DEFAULT 'pending'
                       CHECK (process_status IN ('pending', 'processing', 'done', 'skipped', 'failed')),
    attempts           smallint NOT NULL DEFAULT 0,
    available_at       timestamptz NOT NULL DEFAULT now(),
    last_error         text,
    processed_at       timestamptz,
    servicenow_sys_id  text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (element <> 'system' OR author_type = 'system'),
    CHECK (process_status NOT IN ('done', 'skipped') OR processed_at IS NOT NULL)
);
CREATE INDEX ix_journal_incident_latest ON incident_journal (incident_id, element, created_at DESC);
CREATE INDEX ix_journal_queue ON incident_journal (available_at) WHERE process_status = 'pending';

CREATE TABLE appointments (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            uuid NOT NULL REFERENCES stores(id),
    incident_id         uuid,
    appointment_type    text NOT NULL CHECK (appointment_type IN
                          ('field_tech', 'isp_technician', 'third_party_vendor', 'user_support')),
    title               text NOT NULL,
    purpose             text NOT NULL,
    details             text,
    scheduled_start     timestamptz NOT NULL,
    scheduled_end       timestamptz,
    status              text NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN ('scheduled', 'in_progress', 'completed', 'cancelled', 'no_show')),
    status_note         text,
    vendor_name         text,
    technician_name     text,
    vendor_reference    text,
    created_by          text NOT NULL,
    source              text NOT NULL CHECK (source IN ('servicenow', 'admin_portal', 'agent', 'seed')),
    servicenow_sys_id   text UNIQUE CHECK (servicenow_sys_id ~ '^[0-9a-f]{32}$'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (incident_id, store_id) REFERENCES incidents (id, store_id),
    CHECK (scheduled_end IS NULL OR scheduled_end > scheduled_start),
    CHECK (appointment_type = 'user_support' OR incident_id IS NOT NULL),
    CHECK (status NOT IN ('cancelled', 'no_show') OR status_note IS NOT NULL)
);
CREATE INDEX ix_appt_store_upcoming ON appointments (store_id, scheduled_start)
    WHERE status IN ('scheduled', 'in_progress');
CREATE INDEX ix_appt_portal ON appointments (scheduled_start, status);
CREATE TRIGGER trg_appointments_updated BEFORE UPDATE ON appointments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Group 4: AI workflow
-- =====================================================================
CREATE TABLE ai_prompt_versions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow              text NOT NULL CHECK (workflow IN ('impact_assessment', 'update_classification')),
    version               integer NOT NULL CHECK (version > 0),
    system_prompt         text NOT NULL,
    user_prompt_template  text NOT NULL,
    output_schema         jsonb NOT NULL CHECK (jsonb_typeof(output_schema) = 'object'),
    model_name            text NOT NULL,
    temperature           numeric(3,2) NOT NULL DEFAULT 0 CHECK (temperature BETWEEN 0 AND 2),
    is_active             boolean NOT NULL DEFAULT false,
    change_notes          text,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow, version)
);
CREATE UNIQUE INDEX ux_prompt_one_active ON ai_prompt_versions (workflow) WHERE is_active;

CREATE TABLE ai_workflow_runs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow           text NOT NULL CHECK (workflow IN ('impact_assessment', 'update_classification')),
    journal_id         uuid NOT NULL REFERENCES incident_journal(id),
    incident_id        uuid NOT NULL REFERENCES incidents(id),
    prompt_version_id  uuid NOT NULL REFERENCES ai_prompt_versions(id),
    model_name         text NOT NULL,
    input_context      jsonb NOT NULL,
    output             jsonb,
    outcome            text CHECK (outcome IN ('alert_created', 'action_created',
                         'notification_created', 'no_action', 'invalid_output')),
    status             text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'succeeded', 'failed', 'needs_review')),
    error_message      text,
    prompt_tokens      integer,
    completion_tokens  integer,
    latency_ms         integer,
    started_at         timestamptz NOT NULL DEFAULT now(),
    completed_at       timestamptz,
    CHECK (status = 'running' OR completed_at IS NOT NULL),
    CHECK (status <> 'succeeded' OR (output IS NOT NULL AND outcome IS NOT NULL))
);
CREATE INDEX ix_runs_journal ON ai_workflow_runs (journal_id);
CREATE INDEX ix_runs_incident ON ai_workflow_runs (incident_id, started_at DESC);
CREATE INDEX ix_runs_review ON ai_workflow_runs (started_at DESC) WHERE status IN ('failed', 'needs_review');

-- =====================================================================
-- Group 5: Store communication
-- =====================================================================
CREATE TABLE store_alerts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            uuid NOT NULL REFERENCES stores(id),
    incident_id         uuid NOT NULL UNIQUE,
    title               text NOT NULL,
    detail              text NOT NULL,
    severity            text NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
    source              text NOT NULL CHECK (source IN ('llm', 'system', 'admin_portal', 'seed')),
    ai_workflow_run_id  uuid REFERENCES ai_workflow_runs(id),
    created_by          text NOT NULL,
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    close_reason        text CHECK (close_reason IN ('incident_resolved', 'incident_canceled', 'manual')),
    closed_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (incident_id, store_id) REFERENCES incidents (id, store_id),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL AND close_reason IS NOT NULL)),
    CHECK (source NOT IN ('llm', 'system') OR ai_workflow_run_id IS NOT NULL)
);
CREATE INDEX ix_alert_store_active ON store_alerts (store_id, created_at DESC) WHERE status = 'active';
CREATE INDEX ix_alert_portal ON store_alerts (status, created_at DESC);
CREATE TRIGGER trg_store_alerts_updated BEFORE UPDATE ON store_alerts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE store_actions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id            uuid NOT NULL REFERENCES stores(id),
    incident_id         uuid NOT NULL,
    source_journal_id   uuid REFERENCES incident_journal(id),
    title               text NOT NULL,
    detail              text NOT NULL,
    severity            text NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
    expected_response   text NOT NULL CHECK (expected_response IN ('acknowledge', 'yes_no', 'free_text')),
    due_by              timestamptz,
    source              text NOT NULL CHECK (source IN ('llm', 'admin_portal', 'seed')),
    ai_workflow_run_id  uuid REFERENCES ai_workflow_runs(id),
    created_by          text NOT NULL,
    status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'responded', 'closed')),
    close_reason        text CHECK (close_reason IN ('completed', 'incident_closed', 'expired', 'manual')),
    closed_at           timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, store_id),
    FOREIGN KEY (incident_id, store_id) REFERENCES incidents (id, store_id),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL AND close_reason IS NOT NULL)),
    CHECK (source <> 'llm' OR (ai_workflow_run_id IS NOT NULL AND source_journal_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_action_one_per_journal ON store_actions (source_journal_id)
    WHERE source_journal_id IS NOT NULL;
CREATE INDEX ix_action_store_open ON store_actions (store_id, due_by NULLS LAST) WHERE status = 'open';
CREATE INDEX ix_action_portal ON store_actions (status, created_at DESC);
CREATE TRIGGER trg_store_actions_updated BEFORE UPDATE ON store_actions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE store_action_responses (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id          uuid NOT NULL,
    store_id           uuid NOT NULL,
    response_type      text NOT NULL CHECK (response_type IN ('acknowledge', 'yes_no', 'free_text')),
    answer_yes_no      boolean,
    response_text      text,
    responded_by       text NOT NULL,
    source             text NOT NULL CHECK (source IN ('mobile_app', 'admin_portal', 'seed')),
    client_request_id  uuid UNIQUE,
    journal_id         uuid NOT NULL UNIQUE REFERENCES incident_journal(id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (action_id, store_id) REFERENCES store_actions (id, store_id),
    CHECK (response_type <> 'yes_no' OR answer_yes_no IS NOT NULL),
    CHECK (response_type = 'yes_no' OR answer_yes_no IS NULL),
    CHECK (response_type <> 'free_text' OR length(trim(coalesce(response_text, ''))) > 0)
);
CREATE INDEX ix_resp_action_latest ON store_action_responses (action_id, created_at DESC);

CREATE TABLE notifications (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category           text NOT NULL CHECK (category IN
                         ('maintenance', 'updates', 'incidents', 'appointment', 'training', 'others')),
    title              text NOT NULL,
    description        text NOT NULL,
    link_url           text CHECK (link_url ~ '^https://'),
    source             text NOT NULL CHECK (source IN ('admin_portal', 'system', 'seed')),
    system_event       text CHECK (system_event IN ('incident_update', 'action_required',
                         'appointment_scheduled', 'appointment_rescheduled', 'appointment_cancelled')),
    created_by         text NOT NULL,
    incident_id        uuid REFERENCES incidents(id),
    appointment_id     uuid REFERENCES appointments(id),
    action_id          uuid REFERENCES store_actions(id),
    source_journal_id  uuid UNIQUE REFERENCES incident_journal(id),
    status             text NOT NULL DEFAULT 'published' CHECK (status IN ('draft', 'published', 'withdrawn')),
    publish_at         timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz,
    withdrawn_at       timestamptz,
    withdrawn_by       text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((source = 'system') = (system_event IS NOT NULL)),
    CHECK (expires_at IS NULL OR expires_at > publish_at),
    CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL AND withdrawn_by IS NOT NULL))
);
CREATE INDEX ix_notif_live ON notifications (publish_at DESC) WHERE status = 'published';
CREATE INDEX ix_notif_portal ON notifications (source, category, created_at DESC);
CREATE TRIGGER trg_notifications_updated BEFORE UPDATE ON notifications
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE notification_targets (
    notification_id  uuid NOT NULL REFERENCES notifications(id),
    store_id         uuid NOT NULL REFERENCES stores(id),
    delivered_at     timestamptz,
    read_at          timestamptz,
    read_by          text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (notification_id, store_id),
    CHECK (read_at IS NULL OR delivered_at IS NOT NULL),
    CHECK ((read_at IS NULL) = (read_by IS NULL))
);
CREATE INDEX ix_nt_store ON notification_targets (store_id, notification_id);
CREATE INDEX ix_nt_store_unread ON notification_targets (store_id) WHERE read_at IS NULL;

-- =====================================================================
-- Group 6: Audit and logs
-- =====================================================================
CREATE TABLE audit_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    actor_type   text NOT NULL CHECK (actor_type IN ('it_staff', 'store_user', 'system', 'agent')),
    actor        text NOT NULL,
    source       text NOT NULL CHECK (source IN
                   ('admin_portal', 'mobile_app', 'worker', 'processor', 'agent', 'seed')),
    action       text NOT NULL CHECK (action IN ('create', 'update', 'status_change',
                   'publish', 'withdraw', 'respond', 'deactivate')),
    entity_type  text NOT NULL,
    entity_id    text NOT NULL,
    store_id     uuid REFERENCES stores(id),
    changes      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(changes) = 'object'),
    request_id   uuid
);
CREATE INDEX ix_audit_entity ON audit_log (entity_type, entity_id, occurred_at DESC);
CREATE INDEX ix_audit_store ON audit_log (store_id, occurred_at DESC) WHERE store_id IS NOT NULL;
CREATE INDEX ix_audit_actor ON audit_log (actor, occurred_at DESC);

CREATE TABLE api_request_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id   uuid NOT NULL UNIQUE,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    client_app   text NOT NULL CHECK (client_app IN ('mobile_app', 'admin_portal', 'other')),
    method       text NOT NULL,
    path         text NOT NULL,
    route        text,
    site_code    text,
    actor        text,
    status_code  smallint NOT NULL,
    duration_ms  integer NOT NULL,
    error_code   text
);
CREATE INDEX ix_api_time ON api_request_log (occurred_at DESC);
CREATE INDEX ix_api_site ON api_request_log (site_code, occurred_at DESC) WHERE site_code IS NOT NULL;
CREATE INDEX ix_api_error ON api_request_log (occurred_at DESC) WHERE status_code >= 500;

CREATE TABLE event_processing_runs (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode               text NOT NULL CHECK (mode IN ('rebuild', 'incremental')),
    as_of              timestamptz NOT NULL,
    status             text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    events_read        integer NOT NULL DEFAULT 0,
    events_processed   integer NOT NULL DEFAULT 0,
    duplicates         integer NOT NULL DEFAULT 0,
    unmatched          integer NOT NULL DEFAULT 0,
    errors             integer NOT NULL DEFAULT 0,
    status_changes     integer NOT NULL DEFAULT 0,
    error_message      text,
    triggered_by       text NOT NULL,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    CHECK (status = 'running' OR finished_at IS NOT NULL)
);
