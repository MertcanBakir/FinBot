-- One lease per job protects all trigger keys, including dry runs.
create table private.lambda_job_leases (
    job_name text primary key,
    owner_id uuid not null,
    expires_at timestamptz not null
);

revoke all on private.lambda_job_leases from public, anon, authenticated;
grant select, insert, update, delete on private.lambda_job_leases to service_role;
