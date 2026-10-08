-- Pin the search_path of our functions (Supabase security advisor: function_search_path_mutable).
ALTER FUNCTION today_myt() SET search_path = public;
ALTER FUNCTION generate_rent_schedule(date) SET search_path = public;
