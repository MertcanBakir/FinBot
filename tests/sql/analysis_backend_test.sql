\set ON_ERROR_STOP on
begin;
do $$
declare
    v_role text;
    v_table text;
begin
    foreach v_role in array array['anon','authenticated'] loop
        if has_schema_privilege(v_role,'analysis_backend','USAGE') then
            raise exception 'analysis schema exposed to mobile role';
        end if;
        foreach v_table in array array[
            'connections','jobs','memory','rate_limits','provider_model_catalog',
            'job_model_bindings','schema_revision'
        ] loop
            if has_table_privilege(v_role,'analysis_backend.'||v_table,'SELECT,INSERT,UPDATE,DELETE') then
                raise exception 'analysis table exposed to mobile role';
            end if;
        end loop;
    end loop;
    if (select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='analysis_backend' and c.relkind='r' and c.relrowsecurity) <> 7 then
        raise exception 'analysis RLS missing';
    end if;
    if not exists (select 1 from storage.buckets where id='analysis-reports' and not public) then
        raise exception 'private report bucket missing';
    end if;
    if exists (select 1 from pg_roles where rolname='lucrari_analysis_backend'
        and (rolcanlogin or rolbypassrls or rolsuper)) then
        raise exception 'backend role must not be a login, superuser or bypassrls role';
    end if;
end;
$$;
rollback;
