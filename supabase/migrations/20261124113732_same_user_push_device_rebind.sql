begin;

create or replace function public.register_push_device(
    p_installation_id uuid, p_project_id uuid, p_environment text, p_token text
)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
    v_user uuid := auth.uid();
    v_id uuid;
begin
    if v_user is null then raise exception 'notification_auth_required'; end if;
    if p_installation_id is null or p_project_id is null or p_environment is null
       or p_environment not in ('development', 'production')
       or p_token is null or length(p_token) not between 20 and 256
       or p_token !~ '^(ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]+\]$'
    then raise exception 'notification_invalid_device'; end if;

    -- Serialize registrations for this token and for this user's installation.
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_project_id::text || ':' || p_environment, 0));
    if exists (select 1 from notifications.devices
               where project_id = p_project_id and environment = p_environment
                 and token = p_token and user_id <> v_user) then
        raise exception 'notification_device_conflict';
    end if;

    update notifications.devices
       set installation_id = p_installation_id, active = true, updated_at = now()
     where user_id = v_user and project_id = p_project_id
       and environment = p_environment and token = p_token
    returning id into v_id;

    if v_id is null then
        insert into notifications.devices(user_id, installation_id, project_id, environment, token)
        values(v_user, p_installation_id, p_project_id, p_environment, p_token)
        on conflict(user_id, installation_id, project_id, environment)
        do update set token = excluded.token, active = true, updated_at = now()
        returning id into v_id;
    end if;
    insert into notifications.preferences(user_id) values(v_user) on conflict do nothing;
    return v_id;
exception when unique_violation then
    raise exception 'notification_device_conflict';
end $$;

commit;
