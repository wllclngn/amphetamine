// quark -- Proton replacement launcher
//
// Multi-mode binary:
//   ./proton <verb> <exe>      Proton launcher (Steam compatibility tool)
//   quark package        Package a built Wine tree for Steam
//   quark status         Show project status
//   quark analyze        Analyze Wine source tree
//   quark configure      Optimize Wine build configuration
//   quark clone          Clone Wine/Proton sources
//   quark profile        Profile game performance

#[macro_use]
extern crate triskelion;

mod cli;
mod gaming;
mod clone;
mod status;
mod analyze;
mod configure;
mod profile;
pub(crate) mod launcher;
mod packager;
mod pe_patch;
pub mod pe_scanner;

fn main() {
    match cli::parse_args() {
        cli::Mode::Launch { verb, args } => {
            std::process::exit(launcher::run(&verb, &args));
        }
        cli::Mode::Package { wine_dir } => {
            std::process::exit(packager::run(&wine_dir));
        }
        cli::Mode::Status => {
            std::process::exit(status::run());
        }
        cli::Mode::Analyze => {
            std::process::exit(analyze::run());
        }
        cli::Mode::Configure { wine_dir, execute } => {
            std::process::exit(configure::run(&wine_dir, execute));
        }
        cli::Mode::Profile { app_id, game_name } => {
            std::process::exit(profile::run_profile(&app_id, game_name.as_deref()));
        }
        cli::Mode::ProfileAttach { label } => {
            std::process::exit(profile::run_profile_attach(label.as_deref()));
        }
        cli::Mode::ProfileCompare { dir_a, dir_b } => {
            std::process::exit(profile::run_profile_compare(&dir_a, &dir_b));
        }
        cli::Mode::ProfileOpcodes { trace_file } => {
            std::process::exit(profile::run_profile_opcodes(&trace_file));
        }
        cli::Mode::Clone => {
            clone::ensure_wine_clone();
            clone::ensure_proton_clone();
            log_info!("Both clones ready");
        }
        cli::Mode::Server => {
            log_error!("Server mode is now a separate binary: triskelion");
            std::process::exit(1);
        }
    }
}
