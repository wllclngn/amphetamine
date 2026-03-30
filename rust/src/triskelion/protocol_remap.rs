// Dynamic protocol version remapping
//
// Wine's protocol opcodes are sequential integers assigned by the order of
// @REQ entries in protocol.def. Different Wine/Proton versions reorder,
// add, or remove opcodes, shifting every subsequent number. This module
// detects the client's protocol version at startup and builds a remap table
// so our handlers (keyed by opcode NAME) work with any version.
//
// Flow:
//   1. Detect protocol version from client's ntdll.so binary
//   2. Find/parse the matching protocol.def (cached or fetched)
//   3. Build remap: client_opcode_number → our RequestCode variant
//   4. Dispatch uses this table instead of direct from_i32()

use std::path::{Path, PathBuf};
use crate::protocol::RequestCode;

/// Runtime opcode remap table.
pub struct ProtocolRemap {
    /// Protocol version to send during handshake (what the client expects).
    pub version: u32,
    /// Client opcode number → our RequestCode. None = opcode exists in client
    /// but not in our build (e.g. esync opcodes in Proton that we don't have).
    remap: Vec<Option<RequestCode>>,
    /// Whether this is an identity mapping (no remapping needed).
    pub is_identity: bool,
}

impl ProtocolRemap {
    /// Build a remap table by parsing a protocol.def file and cross-referencing
    /// with our compiled RequestCode enum via from_name().
    pub fn from_protocol_def(def_content: &str, protocol_version: u32) -> Self {
        let client_opcodes = parse_req_names(def_content);
        let count = client_opcodes.len();

        let remap: Vec<Option<RequestCode>> = client_opcodes.iter()
            .map(|name| RequestCode::from_name(name))
            .collect();

        // Build reverse map: our opcode index → client's opcode number
        let our_count = crate::protocol::OPCODE_META.len();
        let mut reverse = vec![None; our_count];
        for (client_idx, name) in client_opcodes.iter().enumerate() {
            if let Some(our_code) = RequestCode::from_name(name) {
                let our_idx = our_code as i32;
                if (our_idx as usize) < our_count {
                    reverse[our_idx as usize] = Some(client_idx as i32);
                }
            }
        }

        let matched = remap.iter().filter(|r| r.is_some()).count();
        let client_only = count - matched;
        let our_only = our_count - reverse.iter().filter(|r| r.is_some()).count();

        log_info!("protocol remap: version={protocol_version} \
                   client_opcodes={count} matched={matched} \
                   client_only={client_only} ours_only={our_only}");

        // Check if this is effectively identity (all opcodes at same positions)
        let is_identity = count == our_count && remap.iter().enumerate().all(|(i, r)| {
            r.map(|c| c as i32 == i as i32).unwrap_or(false)
        });

        if is_identity {
            log_info!("protocol remap: identity (client matches build)");
        }

        Self { version: protocol_version, remap, is_identity }
    }

    /// Identity mapping — client protocol matches our compiled protocol exactly.
    pub fn identity() -> Self {
        let count = crate::protocol::OPCODE_META.len();
        let remap: Vec<Option<RequestCode>> = (0..count as i32)
            .map(|i| RequestCode::from_i32(i))
            .collect();

        Self {
            version: crate::ipc::COMPILED_PROTOCOL_VERSION,
            remap,
            is_identity: true,
        }
    }

    /// Resolve a client's opcode number to our RequestCode.
    #[inline]
    pub fn resolve(&self, client_opcode: i32) -> Option<RequestCode> {
        if client_opcode < 0 {
            return None;
        }
        self.remap.get(client_opcode as usize).copied().flatten()
    }

}

/// Parse @REQ(name) entries from protocol.def, returning names in order.
fn parse_req_names(content: &str) -> Vec<String> {
    content.lines()
        .filter_map(|line| {
            let trimmed = line.trim();
            trimmed.strip_prefix("@REQ(")
                .and_then(|rest| rest.strip_suffix(')'))
                .map(|name| name.to_string())
        })
        .collect()
}

// ── Protocol version detection ───────────────────────────────────────────────

/// Detect the protocol version from a Wine ntdll.so binary by scanning for
/// the "version mismatch" string and extracting the nearby MOV EDX,imm32
/// instruction that loads SERVER_PROTOCOL_VERSION.
pub fn detect_protocol_version(ntdll_path: &Path) -> Option<u32> {
    let data = std::fs::read(ntdll_path).ok()?;

    // Find the "version mismatch" string
    let needle = b"version mismatch";
    let str_offset = find_bytes(&data, needle)?;

    // Find code that references this string via LEA reg,[RIP+disp32].
    // Encoded as: 48 8d XX YY YY YY YY (REX.W LEA)
    // where mod=00, rm=101 (RIP-relative).
    let mut ref_offset = None;
    for pos in 0..data.len().saturating_sub(7) {
        if data[pos] == 0x48 && data[pos + 1] == 0x8d {
            let modrm = data[pos + 2];
            if (modrm & 0xC7) == 0x05 { // mod=00, rm=101
                let disp = i32::from_le_bytes([
                    data[pos + 3], data[pos + 4], data[pos + 5], data[pos + 6],
                ]);
                let target = (pos as i64) + 7 + (disp as i64);
                if target == str_offset as i64 {
                    ref_offset = Some(pos);
                    break;
                }
            }
        }
    }

    let ref_pos = ref_offset?;

    // Search backwards from the string reference for MOV EDX, imm32 (BA xx xx xx xx).
    // This instruction loads SERVER_PROTOCOL_VERSION as the second printf argument.
    // Search within 20 bytes before the LEA.
    for scan in (ref_pos.saturating_sub(20)..ref_pos).rev() {
        if data[scan] == 0xBA { // MOV EDX, imm32
            if scan + 5 <= data.len() {
                let ver = u32::from_le_bytes([
                    data[scan + 1], data[scan + 2], data[scan + 3], data[scan + 4],
                ]);
                if (800..2000).contains(&ver) {
                    return Some(ver);
                }
            }
        }
    }

    None
}

/// Find the first occurrence of `needle` in `haystack`.
fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

// ── Protocol.def resolution ──────────────────────────────────────────────────

/// Find the appropriate protocol.def for a given protocol version.
/// Search order:
///   1. TRISKELION_PROTOCOL_DEF env var (explicit override)
///   2. ~/.local/share/quark/protocols/<version>/protocol.def (cached)
///   3. The build-time Wine source protocol.def
/// Returns (content, version) or None if nothing matches.
pub fn find_protocol_def(target_version: u32) -> Option<(String, u32)> {
    // 1. Explicit override
    if let Ok(path) = std::env::var("TRISKELION_PROTOCOL_DEF") {
        if let Ok(content) = std::fs::read_to_string(&path) {
            log_info!("protocol: using override {path}");
            return Some((content, target_version));
        }
    }

    // 2. Cached protocol.def for this version
    let cache_dir = cache_dir_for_version(target_version);
    let cached_path = cache_dir.join("protocol.def");
    if cached_path.exists() {
        if let Ok(content) = std::fs::read_to_string(&cached_path) {
            log_info!("protocol: loaded cached {}", cached_path.display());
            return Some((content, target_version));
        }
    }

    // 3. Build-time Wine source (matches our compiled protocol)
    if target_version == crate::ipc::COMPILED_PROTOCOL_VERSION {
        // Identity — no remap needed
        return None;
    }

    log_warn!("protocol: no protocol.def found for version {target_version}");
    log_warn!("protocol: cache a protocol.def at {}", cached_path.display());
    None
}

/// Cache directory for protocol files: ~/.local/share/quark/protocols/<version>/
fn cache_dir_for_version(version: u32) -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    PathBuf::from(home)
        .join(".local/share/quark/protocols")
        .join(version.to_string())
}

/// Find Wine's ntdll.so from the Proton/Wine installation.
/// Checks WINELOADER, WINESERVER (our own binary's neighbor), then WINEDLLPATH.
pub fn find_ntdll() -> Option<PathBuf> {
    // Try WINELOADER env var first — the launcher sets this
    if let Ok(loader) = std::env::var("WINELOADER") {
        let loader_path = Path::new(&loader);
        // WINELOADER points to e.g. .../files/bin/wine64
        // ntdll.so is at .../files/lib/wine/x86_64-unix/ntdll.so
        if let Some(bin_dir) = loader_path.parent() {
            if let Some(files_dir) = bin_dir.parent() {
                let ntdll = files_dir.join("lib/wine/x86_64-unix/ntdll.so");
                if ntdll.exists() {
                    return Some(ntdll);
                }
            }
        }
    } else {
        log_warn!("protocol: WINELOADER not set");
    }

    // Try WINEDLLPATH — the launcher sets this to point at the Wine lib dirs.
    // Format: "/path/to/lib/wine/x86_64-unix:/path/to/lib/wine/x86_64-windows"
    if let Ok(dll_path) = std::env::var("WINEDLLPATH") {
        for dir in dll_path.split(':') {
            let ntdll = Path::new(dir).join("ntdll.so");
            if ntdll.exists() {
                return Some(ntdll);
            }
        }
    }

    // Try PROTON_PATH env var
    if let Ok(proton_path) = std::env::var("PROTON_PATH") {
        let ntdll = Path::new(&proton_path).join("files/lib/wine/x86_64-unix/ntdll.so");
        if ntdll.exists() {
            return Some(ntdll);
        }
    }

    // Try our own binary's neighbor — if triskelion is deployed next to Wine
    if let Ok(self_exe) = std::env::current_exe() {
        if let Some(self_dir) = self_exe.parent() {
            let ntdll = self_dir.join("lib/wine/x86_64-unix/ntdll.so");
            if ntdll.exists() {
                return Some(ntdll);
            }
        }
    }

    None
}

// ── Top-level detection and remap construction ───────────────────────────────

/// Detect the client's protocol version and build the appropriate remap table.
/// Called once at daemon startup before accepting connections.
pub fn detect_and_remap() -> ProtocolRemap {
    let compiled = crate::ipc::COMPILED_PROTOCOL_VERSION;

    // Try to find ntdll.so and detect client protocol version
    let detected_version = find_ntdll()
        .and_then(|path| {
            detect_protocol_version(&path)
        });

    match detected_version {
        Some(ver) if ver == compiled => {
            log_info!("protocol: client version {ver} matches build — no remap needed");
            ProtocolRemap::identity()
        }
        Some(ver) => {
            log_info!("protocol: client version {ver}, build version {compiled} — remapping");
            match find_protocol_def(ver) {
                Some((content, version)) => ProtocolRemap::from_protocol_def(&content, version),
                None => {
                    log_error!("protocol: no protocol.def for version {ver}, \
                               using build protocol (version {compiled}). Expect crashes if opcodes differ.");
                    ProtocolRemap::identity()
                }
            }
        }
        None => {
            log_warn!("protocol: could not detect client version, using build ({compiled})");
            ProtocolRemap::identity()
        }
    }
}
