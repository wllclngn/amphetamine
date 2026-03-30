// TOML configuration for PARALLAX
//
// Default location: ~/.config/parallax/parallax.toml
// Override via --config flag or PARALLAX_CONFIG env var.

use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct Config {
    #[serde(default = "default_multiplier")]
    pub multiplier: f64,

    #[serde(default)]
    pub child: Option<String>,

    #[serde(default)]
    pub output: Vec<OutputOverride>,
}

#[derive(Debug, Deserialize)]
pub struct OutputOverride {
    pub r#match: String,
    #[serde(default = "default_multiplier")]
    pub multiplier: f64,
}

fn default_multiplier() -> f64 { 1.0 }

impl Default for Config {
    fn default() -> Self {
        Config {
            multiplier: 1.0,
            child: None,
            output: Vec::new(),
        }
    }
}

impl Config {
    pub fn load() -> Config {
        // Check --config arg, then env, then default path
        let path = std::env::args().skip_while(|a| a != "--config")
            .nth(1)
            .or_else(|| std::env::var("PARALLAX_CONFIG").ok())
            .unwrap_or_else(|| {
                let home = std::env::var("HOME").unwrap_or_default();
                format!("{home}/.config/parallax/parallax.toml")
            });

        match std::fs::read_to_string(&path) {
            Ok(contents) => {
                match toml::from_str(&contents) {
                    Ok(config) => config,
                    Err(e) => {
                        eprintln!("[PARALLAX] config parse error: {e}");
                        Config::default()
                    }
                }
            }
            Err(_) => Config::default(),
        }
    }

    pub fn multiplier_for(&self, connector_name: &str, monitor_name: Option<&str>) -> f64 {
        for ov in &self.output {
            if connector_name.contains(&ov.r#match) {
                return ov.multiplier;
            }
            if let Some(name) = monitor_name {
                if name.contains(&ov.r#match) {
                    return ov.multiplier;
                }
            }
        }
        self.multiplier
    }
}
