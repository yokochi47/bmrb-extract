--
-- Development-only schema reset.
-- Drops every service table and enum type so the CREATE statements in
-- init-service.sql.template (appended after this file by config.sh) can
-- recreate a clean schema. Applied via reset_db.sh against the running
-- postgres container; never runs in production.
--

\c internal;

DROP TABLE IF EXISTS workflow, communication, notification, output_file, upload_file, session CASCADE;
DROP TYPE IF EXISTS wf_status_code, wf_task_code, delivery_status_code,
  output_file_type, upload_file_type, target_depsys_code,
  session_status_code, processing_site_code CASCADE;

DROP TYPE IF EXISTS processing_site_code;
CREATE TYPE processing_site_code AS ENUM ('bmrb.io', 'pdbj.org');
DROP TYPE IF EXISTS session_status_code;
CREATE TYPE session_status_code AS ENUM ('created', 'uploading', 'processing', 'completed', 'failed', 'expired');
DROP TYPE IF EXISTS target_depsys_code;
CREATE TYPE target_depsys_code AS ENUM ('onedep', 'repl_cs', 'bmrbdep');

--
-- Represents one UI session.
--

CREATE TABLE IF NOT EXISTS session (
    processing_site     processing_site_code DEFAULT 'pdbj.org',  -- the original processing site, which created the session.
    token               UUID PRIMARY KEY DEFAULT uuidv7(),  -- token used by user and web frontend, uuidv7() func requires PostgreSQL 18+.
    token_admin         UUID DEFAULT gen_random_uuid(),     -- token used by administrators to respond to inquiries on 'Communication' page.
    token_expiry        TIMESTAMP NOT NULL,                 -- time of expiry of the session.

    consented           BOOLEAN NOT NULL DEFAULT FALSE,     -- whether user consented to our policies: 'Terms and Conditions' and 'Privacy Policy'

    client_ip           INET,
    user_agent          TEXT,

    status              session_status_code NOT NULL DEFAULT 'created',

    -- The selection of target deposition system defines file upload requirements.
    -- onedep (conventional): coordinates, assigned chemical shifts, NMR restraints
    --        (combined)    : coordinates, NMR unified data
    -- repl_cs         : coordinates processed by OneDep, NMR unified data processed by OneDep, correct assigned chemical shifts
    -- bmrbdep         : assigned chemical shifts
    -- where the coordinate file must be in either PDBx/mmCIF format or legacy PDB format,
    --       the NMR unified data file must be a single file containng assigned chemical shifts and NMR restraints.
    target_depsys       target_depsys_code NOT NULL DEFAULT 'onedep',
    related_bmrb_id     INT CHECK ( related_bmrb_id > 0 and related_bmrb_id < 100000 ), -- onedep mode only

    -- The actual 'Conversion ID' will be generated from a prefix 'C_' and conversion_id.
    -- For instance,
    -- UConn production server (bmrb-extract.bmrb.io) : C_1xxxxxx
    -- Osaka production server (bmrb-extract.pdbj.org): C_2xxxxxx
    -- Anonymous development server                   : C_8xxxxxx
    conversion_id       INT CHECK ( conversion_id >= 1000000 and conversion_id < 9000000 ) UNIQUE,

    -- Number of committed processing runs (0 before the first 'Process selected files').
    -- The in-progress draft run is always latest_run_number + 1.
    latest_run_number   INT NOT NULL DEFAULT 0 CHECK ( latest_run_number >= 0 ),

    created_at          TIMESTAMP DEFAULT now(), -- timestamp when the session was created according to the user policy agreement.
    started_at          TIMESTAMP,  -- timestamp when conversion processes starts. ('Process selected files' button on 'File upload' page)
    finished_at         TIMESTAMP,  -- timestamp when conversion processes finishes.

    approved            BOOLEAN DEFAULT FALSE,  -- wheather user acknowledges all warnings, allowing user to download the conversion results.
    exchanged           BOOLEAN DEFAULT FALSE,  -- wheather data exchange among production servers completes.
    downloaded          BOOLEAN DEFAULT FALSE   -- wheather user downloads the conversion results.
);

DROP TYPE IF EXISTS upload_file_type;
CREATE TYPE upload_file_type AS ENUM (
    'co-cif', 'co-pdb',
    'nm-aux-amb', 'nm-aux-cha', 'nm-aux-gro', 'nm-aux-pdb', 'nm-aux-xea',
    'nm-csp-ari', 'nm-csp-bar', 'nm-csp-gar', 'nm-csp-npi',
    'nm-csp-oli', 'nm-csp-pip', 'nm-csp-ppm', 'nm-csp-st2', 'nm-csp-xea',
    'nm-shi',     'nm-shi-ari', 'nm-shi-bar', 'nm-shi-gar', 'nm-shi-npi',
    'nm-shi-oli', 'nm-shi-pip', 'nm-shi-ppm', 'nm-shi-st2', 'nm-shi-xea',
    'nm-pea-any', 'nm-pea-ari', 'nm-pea-bar', 'nm-pea-ccp', 'nm-pea-oli',
    'nm-pea-pip', 'nm-pea-pon', 'nm-pea-spa', 'nm-pea-sps', 'nm-pea-top',
    'nm-pea-vie', 'nm-pea-vnm', 'nm-pea-xea', 'nm-pea-xwi',
    'nm-res-amb', 'nm-res-ari', 'nm-res-arx', 'nm-res-bar', 'nm-res-bio',
    'nm-res-cha', 'nm-res-cns', 'nm-res-cya', 'nm-res-dyn', 'nm-res-gro',
    'nm-res-isd', 'nm-res-noa', 'nm-res-oth', 'nm-res-ros', 'nm-res-sax',
    'nm-res-sch', 'nm-res-syb', 'nm-res-xpl',
    'nm-uni-nef', 'nm-uni-str'
    --
    -- See also https://github.com/wwPDB/py-wwpdb_utils_nmr/blob/master/wwpdb/utils/nmr/README.md
    --
);

DROP TYPE IF EXISTS upload_file_source;
CREATE TYPE upload_file_source AS ENUM (
    'user',  -- uploaded by the user
    'bmrb'   -- downloaded from BMRB for a valid related_bmrb_id (onedep conventional mode)
);

--
-- Uploaded files.
--

CREATE TABLE IF NOT EXISTS upload_file (
    token           UUID REFERENCES session(token) ON DELETE CASCADE,
    ordinal         INT CHECK ( ordinal > 0 ),

    conversion_id   INT REFERENCES session(conversion_id),  -- conversion_id is null until the conversion process begins so that conversion_id can not be a part of composite primary key.

    -- Provenance marker: the draft run number (session.latest_run_number + 1) at upload time.
    -- Files accumulate across runs and are never auto-deleted once a run has used them; 'selected'
    -- controls participation in the next run.
    run_number      INT NOT NULL DEFAULT 1 CHECK ( run_number > 0 ),

    original_name   TEXT NOT NULL,
    stored_path     TEXT NOT NULL,  -- if user uploads different files with the same name, the save paths for those files must be different.

    file_size       BIGINT,
    checksum        TEXT,

    file_type       upload_file_type,  -- null until the user assigns a type to a freshly uploaded file.

    selected        BOOLEAN NOT NULL DEFAULT TRUE,  -- selected files will be used for the conversion process.

    source          upload_file_source NOT NULL DEFAULT 'user',  -- 'bmrb' rows are fetched from BMRB, not uploaded by the user.

    uploaded_at     TIMESTAMP DEFAULT now(),

    PRIMARY KEY ( token, ordinal )
);

DROP TYPE IF EXISTS output_file_type;
CREATE TYPE output_file_type AS ENUM (
    'pdbx', 'nmr-star', 'nef',
    'json_report', 'pdf_report', 'text_report',
    'compressed'
);

--
-- Output files.
--

CREATE TABLE IF NOT EXISTS output_file (
    conversion_id   INT REFERENCES session(conversion_id) ON DELETE CASCADE,
    run_number      INT NOT NULL DEFAULT 1 CHECK ( run_number > 0 ),  -- the processing run that produced this output.
    ordinal         INT CHECK ( ordinal > 0 ),

    stored_path     TEXT NOT NULL,

    file_size       BIGINT,
    checksum        TEXT,

    file_type       output_file_type NOT NULL,

    downloaded      BOOLEAN NOT NULL DEFAULT TRUE,

    downloaded_at   TIMESTAMP DEFAULT now(),

    client_ip       INET,
    user_agent      TEXT,

    PRIMARY KEY ( conversion_id, run_number, ordinal )
);

DROP TYPE IF EXISTS delivery_status_code;
CREATE TYPE delivery_status_code AS ENUM ('sent', 'queued', 'failed');

--
-- Notification.
--

CREATE TABLE IF NOT EXISTS notification (
    conversion_id   INT REFERENCES session(conversion_id) ON DELETE CASCADE,
    ordinal         INT CHECK ( ordinal > 0 ),

    subject         TEXT NOT NULL,
    content         TEXT NOT NULL,

    sent_at         TIMESTAMP DEFAULT now(),

    delivery_status delivery_status_code,

    PRIMARY KEY ( conversion_id, ordinal )
);

--
-- Communication.
--

CREATE TABLE IF NOT EXISTS communication (
    conversion_id   INT REFERENCES session(conversion_id) ON DELETE CASCADE,
    ordinal         INT CHECK ( ordinal > 0 ),

    subject         TEXT NOT NULL,
    content         TEXT NOT NULL,

    email_address   TEXT NOT NULL,

    sent_at         TIMESTAMP DEFAULT now(),

    delivery_status delivery_status_code,

    PRIMARY KEY ( conversion_id, ordinal )
);

DROP TYPE IF EXISTS wf_task_code;
CREATE TYPE wf_task_code as ENUM ('issue_conversion', 'convert_model', 'convert_nmr_data', 'nef_release', 'convert_pdf', 'session_exchange', 'session_lock', 'session_cleanup', 'notification', 'communication');

DROP TYPE IF EXISTS wf_stats_code;
CREATE TYPE wf_status_code as ENUM ('created', 'pending', 'aborted', 'processing', 'completed', 'failed');

--
-- Workflow tracking.
--

CREATE TABLE IF NOT EXISTS workflow (
    conversion_id   INT REFERENCES session(conversion_id) ON DELETE CASCADE,
    run_number      INT NOT NULL DEFAULT 1 CHECK ( run_number > 0 ),  -- the processing run this task belongs to.
    ordinal         INT CHECK ( ordinal > 0 ),

    task            wf_task_code NOT NULL,

    status          wf_status_code NOT NULL DEFAULT 'created',
    log_path        TEXT NOT NULL,

    -- NmrDpUtility report analysis (convert_nmr_data): OK | Warning | Error.
    -- 'Error' is a blocker (user must fix and re-upload). report_summary holds the
    -- HTML error/warning summary for the frontend (NULL when status is OK).
    report_status   TEXT,
    report_summary  TEXT,

    created_at      TIMESTAMP DEFAULT now(),
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    expiry_at       TIMESTAMP,

    PRIMARY KEY ( conversion_id, run_number, ordinal )
);

