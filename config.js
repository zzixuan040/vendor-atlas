// Shared workflow storage for Vendor Atlas.
//
// Vendor *facts* live in vendors.json (git). Human *workflow* state — status,
// owner, priority, notes, next action, activity — lives in Supabase so the whole
// sourcing team sees the same board.
//
// Leave these blank and the app still runs, storing workflow state in this
// browser only (clearly labelled, not shared). Fill them in to turn on team sync:
//
//   1. Create a free project at https://supabase.com
//   2. Run supabase-setup.sql in the project's SQL editor
//   3. Paste the Project URL and the anon/public key below, commit, push
//
// The anon key is a public client key by design — it is safe to commit. It is
// NOT a service-role key; never paste a service_role key here.
window.VENDOR_ATLAS_CONFIG = {
  supabaseUrl: "",
  supabaseAnonKey: ""
};
