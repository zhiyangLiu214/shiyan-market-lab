CREATE DATABASE IF NOT EXISTS shiyan_research CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
USE shiyan_research;

CREATE TABLE IF NOT EXISTS datasets (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    name VARCHAR(190) NOT NULL UNIQUE,
    kind ENUM('historical-cache') NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(128) NOT NULL,
    sealed_at TIMESTAMP NULL,
    manifest_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS files (
    dataset_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    path VARCHAR(512) NOT NULL,
    sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source TEXT,
    source_time TEXT,
    retrieved_at TEXT,
    content LONGBLOB NOT NULL,
    PRIMARY KEY (dataset_id, path),
    CONSTRAINT files_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS strategy_bundles (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    name VARCHAR(190) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    content LONGBLOB NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS runs (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    dataset_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    owner_name VARCHAR(128) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    report JSON NOT NULL,
    CONSTRAINT runs_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id)
) ENGINE=InnoDB;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW own_runs AS
    SELECT id, dataset_id, owner_name, created_at, report FROM runs
    WHERE owner_name = SUBSTRING_INDEX(USER(), '@', 1);

DELIMITER //
DROP PROCEDURE IF EXISTS add_own_run//
CREATE PROCEDURE add_own_run(IN run_id CHAR(36), IN input_dataset CHAR(36), IN input_report JSON)
SQL SECURITY DEFINER
BEGIN
    INSERT INTO runs (id, dataset_id, owner_name, report)
    VALUES (run_id, input_dataset, SUBSTRING_INDEX(USER(), '@', 1), input_report);
END//
DELIMITER ;
