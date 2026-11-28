alter table private.job_execution_keys
    drop constraint job_execution_keys_status_check;

alter table private.job_execution_keys
    add constraint job_execution_keys_status_check
    check (status in ('running', 'success', 'partial', 'failed', 'skipped'));
