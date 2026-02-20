-- Migrate template database names: oduflow_{id}_template_{name} → oduflow_template_{id}_{name}
-- Run as superuser: psql -U odoo -f migrate_db_names.sql

-- Unmark templates so they can be renamed
UPDATE pg_database SET datistemplate = false
 WHERE datname IN ('oduflow_1_template_prod', 'oduflow_2_template_18.0-exten-transfer', 'oduflow_2_template_connect18');

-- Terminate active connections
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname IN ('oduflow_1_template_prod', 'oduflow_2_template_18.0-exten-transfer', 'oduflow_2_template_connect18')
   AND pid <> pg_backend_pid();

-- Rename
ALTER DATABASE "oduflow_1_template_prod"                RENAME TO "oduflow_template_1_prod";
ALTER DATABASE "oduflow_2_template_18.0-exten-transfer" RENAME TO "oduflow_template_2_18.0-exten-transfer";
ALTER DATABASE "oduflow_2_template_connect18"           RENAME TO "oduflow_template_2_connect18";

-- Re-mark as templates
UPDATE pg_database SET datistemplate = true WHERE datname LIKE 'oduflow\_template\_%';
