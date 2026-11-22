\set ON_ERROR_STOP on
begin;
do $$
declare
    v_user uuid := gen_random_uuid();
    v_job uuid := gen_random_uuid();
    v_role text;
begin
    insert into auth.users(id) values (v_user);
    insert into analysis_backend.jobs
        (id,user_id,request_key,request_hash,request,model_settings,
         auth_type,status,lease_until,hard_deadline)
    values
        (v_job,v_user,gen_random_uuid(),'fixture','{}',
         '{"quick":{"provider":"chatgpt"},"deep":{"provider":"chatgpt"}}',
         'personal_chatgpt','reserved',clock_timestamp()+interval '1 minute',
         clock_timestamp()+interval '1 hour');
    begin
        update analysis_backend.jobs set model_settings='{}' where id=v_job;
        raise exception 'Job configuration was unexpectedly mutable';
    exception when check_violation then null;
    end;
    foreach v_role in array array['anon','authenticated'] loop
        if has_schema_privilege(v_role,'analysis_backend','USAGE')
           or has_table_privilege(v_role,'analysis_backend.jobs','SELECT,INSERT,UPDATE,DELETE') then
            raise exception 'Personal migration widened mobile privileges';
        end if;
    end loop;
    if not exists (select 1 from pg_indexes where schemaname='analysis_backend'
                   and indexname='analysis_jobs_reserved_created') then
        raise exception 'Reservation discovery index missing';
    end if;
end;
$$;
rollback;
