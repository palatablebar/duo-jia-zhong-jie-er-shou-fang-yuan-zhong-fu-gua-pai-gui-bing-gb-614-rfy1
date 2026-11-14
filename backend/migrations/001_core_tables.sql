-- 核心表结构参考（MySQL 8.0 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）

CREATE TABLE users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    email           VARCHAR(160) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(80)  NOT NULL,
    role            VARCHAR(20)  NOT NULL,
    org             VARCHAR(120),
    created_at      DATETIME     NOT NULL
);

CREATE TABLE agencies (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    code            VARCHAR(40)  NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    kind            VARCHAR(20)  NOT NULL,           -- broker / platform / feed
    endpoint        VARCHAR(255),
    owner           VARCHAR(80),
    note            TEXT,
    config          JSON,
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    last_synced_at  DATETIME,
    created_at      DATETIME     NOT NULL,
    updated_at      DATETIME     NOT NULL
);

CREATE TABLE match_runs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    run_no          VARCHAR(40)  NOT NULL UNIQUE,
    `trigger`       VARCHAR(20)  NOT NULL,           -- manual / schedule / seed
    status          VARCHAR(20)  NOT NULL,
    operator_email  VARCHAR(160) NOT NULL,
    listing_count   INT          NOT NULL DEFAULT 0,
    cluster_count   INT          NOT NULL DEFAULT 0,
    multi_count     INT          NOT NULL DEFAULT 0,
    dedup_rate      DOUBLE       NOT NULL DEFAULT 0,
    risk_count      INT          NOT NULL DEFAULT 0,
    threshold       DOUBLE       NOT NULL DEFAULT 0.8,
    rule_version    VARCHAR(24)  NOT NULL DEFAULT 'v1_threshold', -- v1_threshold / v2_composite
    metrics         JSON,
    log_text        TEXT,
    seed_key        VARCHAR(120) UNIQUE,
    started_at      DATETIME     NOT NULL,
    finished_at     DATETIME
);

-- 规则切换（v1 单阈值 → v2 复合门控）整批回填任务与断点检查点
CREATE TABLE cutover_jobs (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    job_key             VARCHAR(80)  NOT NULL UNIQUE,
    from_version        VARCHAR(24)  NOT NULL DEFAULT 'v1_threshold',
    to_version          VARCHAR(24)  NOT NULL DEFAULT 'v2_composite',
    status              VARCHAR(16)  NOT NULL,        -- pending/running/failed/completed/rolled_back
    backfill_days       INT          NOT NULL DEFAULT 30,
    total_listings      INT          NOT NULL DEFAULT 0,
    total_batches       INT          NOT NULL DEFAULT 0,
    done_batches        INT          NOT NULL DEFAULT 0,
    cluster_count       INT          NOT NULL DEFAULT 0,
    member_count        INT          NOT NULL DEFAULT 0,
    export_rows         INT          NOT NULL DEFAULT 0,
    review_carried      INT          NOT NULL DEFAULT 0,
    review_invalidated  INT          NOT NULL DEFAULT 0,
    review_requeued     INT          NOT NULL DEFAULT 0,
    reconciliation      JSON,
    last_error          TEXT,
    started_at          DATETIME     NOT NULL,
    finished_at         DATETIME,
    created_at          DATETIME     NOT NULL,
    updated_at          DATETIME     NOT NULL
);

CREATE TABLE cutover_checkpoints (
    id                    INT AUTO_INCREMENT PRIMARY KEY,
    job_id                INT          NOT NULL,
    batch_no              INT          NOT NULL,
    status                VARCHAR(12)  NOT NULL,      -- pending/done/failed
    phase                 VARCHAR(16)  NOT NULL,      -- normalize/resolve/persist/review/done
    listing_ids           JSON,
    cluster_fingerprints  JSON,
    attempts              INT          NOT NULL DEFAULT 0,
    error                 TEXT,
    started_at            DATETIME,
    finished_at           DATETIME,
    created_at            DATETIME     NOT NULL,
    updated_at            DATETIME     NOT NULL,
    CONSTRAINT uq_cutover_checkpoint_batch UNIQUE (job_id, batch_no),
    CONSTRAINT fk_checkpoint_job FOREIGN KEY (job_id) REFERENCES cutover_jobs(id) ON DELETE CASCADE
);

CREATE TABLE listings (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    agency_id        INT          NOT NULL,
    lid              VARCHAR(40)  NOT NULL,
    community_raw    VARCHAR(160) NOT NULL,
    layout_raw       VARCHAR(60)  NOT NULL,
    area_raw         VARCHAR(60)  NOT NULL,
    price_raw        VARCHAR(60)  NOT NULL,
    floor_raw        VARCHAR(60)  NOT NULL,
    orientation_raw  VARCHAR(40)  NOT NULL,
    title            VARCHAR(200),
    status           VARCHAR(20)  NOT NULL DEFAULT 'active',
    listed_days      INT          NOT NULL DEFAULT 0,
    views            INT          NOT NULL DEFAULT 0,
    captured_at      DATETIME     NOT NULL,
    community_norm   VARCHAR(160),
    layout_norm      VARCHAR(40),
    area_num         DOUBLE,
    price_num        INT,
    floor_num        INT,
    floor_total      INT,
    floor_band       VARCHAR(10),
    orientation_norm VARCHAR(20),
    title_tokens     JSON,
    parse_flags      JSON,
    property_id      INT,
    risk_tags        JSON,
    created_at       DATETIME     NOT NULL,
    updated_at       DATETIME     NOT NULL,
    CONSTRAINT uq_listings_agency_lid UNIQUE (agency_id, lid),
    CONSTRAINT fk_listings_agency FOREIGN KEY (agency_id) REFERENCES agencies(id) ON DELETE CASCADE
);

CREATE TABLE properties (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    cluster_key  VARCHAR(64)  NOT NULL UNIQUE,
    community    VARCHAR(160) NOT NULL,
    layout       VARCHAR(40)  NOT NULL,
    area_mode    DOUBLE,
    area_min     DOUBLE,
    area_max     DOUBLE,
    price_median INT,
    price_min    INT,
    price_max    INT,
    price_p10    INT,
    price_p90    INT,
    spread       INT,
    dispersion   DOUBLE,
    agency_count INT,
    member_count INT,
    confidence   DOUBLE,
    risk_score   INT          NOT NULL DEFAULT 0,
    risk_level   VARCHAR(10)  NOT NULL DEFAULT 'none',
    risk_tags    JSON,
    risk_detail  JSON,
    member_lids  JSON,
    rule_version VARCHAR(24)  NOT NULL DEFAULT 'v1_threshold', -- v1_threshold / v2_composite
    publish_state VARCHAR(12) NOT NULL DEFAULT 'active',       -- active 已发布 / staging 回填半成品 / archived 归档
    member_fingerprint VARCHAR(64),
    cutover_job_id INT,
    run_id       INT,
    seeded_at    DATETIME     NOT NULL,
    created_at   DATETIME     NOT NULL,
    updated_at   DATETIME     NOT NULL,
    CONSTRAINT fk_properties_run FOREIGN KEY (run_id) REFERENCES match_runs(id) ON DELETE SET NULL,
    CONSTRAINT fk_properties_cutover FOREIGN KEY (cutover_job_id) REFERENCES cutover_jobs(id) ON DELETE SET NULL
);

ALTER TABLE listings
    ADD CONSTRAINT fk_listings_property FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE SET NULL;

CREATE TABLE reviews (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    property_id          INT          NOT NULL,
    run_id               INT,
    reason               VARCHAR(20)  NOT NULL,              -- bait / stale / spread / manual
    severity             VARCHAR(10)  NOT NULL,
    risk_score           INT          NOT NULL DEFAULT 0,
    status               VARCHAR(16)  NOT NULL DEFAULT 'pending',
    reviewer             VARCHAR(160),
    note                 TEXT,
    seed_key             VARCHAR(120) UNIQUE,
    member_fingerprint   VARCHAR(64),
    rule_version         VARCHAR(24)  NOT NULL DEFAULT 'v1_threshold',
    invalidated_at       DATETIME,                          -- 非空即旧结论已作废
    invalid_reason       VARCHAR(24),                       -- cluster_split / members_changed
    superseded_by_id     INT,
    recheck_of_id        INT,
    created_at           DATETIME     NOT NULL,
    updated_at           DATETIME     NOT NULL,
    CONSTRAINT fk_reviews_property FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    CONSTRAINT fk_reviews_run FOREIGN KEY (run_id) REFERENCES match_runs(id) ON DELETE SET NULL
);

CREATE TABLE audit_logs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    actor_email     VARCHAR(160) NOT NULL,
    actor_role      VARCHAR(20)  NOT NULL,
    action          VARCHAR(64)  NOT NULL,
    entity          VARCHAR(40)  NOT NULL,
    entity_id       VARCHAR(64),
    detail          JSON,
    layer           VARCHAR(16)  NOT NULL DEFAULT 'domain',
    ip              VARCHAR(64),
    seed_key        VARCHAR(120) UNIQUE,
    created_at      DATETIME     NOT NULL
);
