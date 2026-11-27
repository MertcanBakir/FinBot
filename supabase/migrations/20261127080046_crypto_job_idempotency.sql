begin;

create table private.job_execution_keys (
    job_name text not null,
    execution_key text not null,
    status text not null check (status in ('running', 'success', 'partial', 'failed')),
    attempt_count integer not null default 1 check (attempt_count > 0),
    started_at timestamptz not null default clock_timestamp(),
    finished_at timestamptz,
    primary key (job_name, execution_key)
);

revoke all on private.job_execution_keys from public, anon, authenticated;
grant select, insert, update on private.job_execution_keys to service_role;

commit;
